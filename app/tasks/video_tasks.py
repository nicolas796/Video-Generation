"""Async video assembly tasks using Celery."""
import os
import json
import subprocess
from typing import Any, Dict, Optional

from celery import states
from celery.exceptions import SoftTimeLimitExceeded
from flask import current_app

from app import db
from app.models import UseCase, Script, VideoClip, Hook
from app.celery_app import celery


@celery.task(bind=True, max_retries=3)
def assemble_final_video_async(
    self,
    use_case_id: int,
    script_id: int,
    options: Optional[Dict[str, Any]] = None,
    upload_root: Optional[str] = None
) -> Dict[str, Any]:
    """Assemble final video in the background.
    
    This task runs the video assembly process asynchronously,
    updating progress as it goes.
    
    Args:
        self: Celery task instance (for progress updates)
        use_case_id: ID of the use case
        script_id: ID of the script
        options: Dict with assembly options:
            - transition: str (cut, fade, etc.)
            - quality: str (low, medium, high)
            - include_voiceover: bool
            - voiceover_path: str (optional)
            - background_music: str (optional)
            - transition_duration: float
            - format_override: str (optional)
        upload_root: Absolute path to the uploads directory determined by the web
            request. Passing this prevents Celery from falling back to a different
            default when running in a separate container.
    
    Returns:
        Dict with result info
    """
    from app.services.smart_assembly import SmartVideoAssembler
    from app.services.voiceover import VoiceoverGenerator
    
    options = options or {}
    configured_upload = upload_root or current_app.config.get('UPLOAD_FOLDER', './uploads')
    upload_folder = os.path.abspath(configured_upload)
    ffmpeg_path = current_app.config.get('FFMPEG_PATH', 'ffmpeg')
    current_app.logger.info('Assembly task upload_folder=%s, exists=%s', upload_folder, os.path.exists(upload_folder))

    if upload_root and upload_root != current_app.config.get('UPLOAD_FOLDER'):
        current_app.logger.info(
            "Celery task using explicit upload_root",
            extra={
                'provided_upload_root': upload_root,
                'app_upload_folder': current_app.config.get('UPLOAD_FOLDER')
            }
        )
    
    try:
        # Update status to STARTED
        self.update_state(
            state=states.STARTED,
            meta={
                'step': 'initializing',
                'progress': 5,
                'message': 'Starting video assembly...'
            }
        )
        
        # Get use case and script
        use_case = db.session.get(UseCase, use_case_id)
        script = db.session.get(Script, script_id)
        
        if not use_case or not script:
            raise ValueError("Use case or script not found")
        
        if script.status != 'approved':
            raise ValueError("Script must be approved before assembly")
        
        # Download any clips that have Pollo URLs but no local file
        self.update_state(
            state=states.STARTED,
            meta={
                'step': 'downloading_clips',
                'progress': 8,
                'message': 'Downloading video clips...'
            }
        )
        
        from app.utils.clip_assets import download_clip_assets
        from app.services.pollo_ai import PolloAIClient

        # Get all clips that could be used in assembly (ready or complete).
        # The web service may have already polled Pollo and set file_path, but
        # that file lives on the web service's disk, not the worker's.  We must
        # check actual file existence on the local filesystem.
        assembly_clips = VideoClip.query.filter(
            VideoClip.use_case_id == use_case_id,
            VideoClip.status.in_(['ready', 'complete'])
        ).all()

        downloaded_count = 0
        pollo_client = None  # lazy-init only if needed
        for clip in assembly_clips:
            # Resolve the video URL — may need to fetch from Pollo
            video_url = clip.pollo_video_url
            if not video_url and clip.pollo_job_id:
                try:
                    if pollo_client is None:
                        pollo_client = PolloAIClient()
                    status_result = pollo_client.check_job_status(clip.pollo_job_id, clip=clip)
                    if status_result.get('success'):
                        video_url = pollo_client._extract_video_url(status_result.get('result'))
                        if video_url:
                            clip.pollo_video_url = video_url
                            current_app.logger.info(
                                "Resolved video URL from Pollo for clip missing pollo_video_url",
                                extra={'clip_id': clip.id, 'pollo_job_id': clip.pollo_job_id}
                            )
                except Exception as e:
                    current_app.logger.warning(
                        "Failed to fetch video URL from Pollo",
                        extra={'clip_id': clip.id, 'pollo_job_id': clip.pollo_job_id, 'error': str(e)}
                    )

            if not video_url:
                current_app.logger.warning(
                    "Clip has no video URL and could not resolve one, skipping",
                    extra={'clip_id': clip.id, 'pollo_job_id': clip.pollo_job_id}
                )
                continue

            # Check whether the file actually exists on this worker's disk
            needs_download = False
            if not clip.file_path:
                needs_download = True
            else:
                resolved = os.path.join(upload_folder, clip.file_path)
                if not os.path.exists(resolved):
                    needs_download = True
                    current_app.logger.info(
                        "Clip file_path set but file missing on worker disk, re-downloading",
                        extra={
                            'clip_id': clip.id,
                            'file_path': clip.file_path,
                            'resolved': resolved,
                        }
                    )
            if needs_download:
                try:
                    current_app.logger.info(
                        "Downloading clip in worker",
                        extra={
                            'clip_id': clip.id,
                            'use_case_id': use_case_id,
                            'pollo_video_url': video_url[:100]
                        }
                    )
                    assets = download_clip_assets(
                        clip=clip,
                        video_url=video_url,
                        upload_root=upload_folder,
                        logger=current_app.logger
                    )
                    clip.file_path = assets['video']
                    clip.thumbnail_path = assets['thumbnail']
                    clip.status = 'complete'
                    downloaded_count += 1
                except Exception as e:
                    current_app.logger.error(
                        "Failed to download clip",
                        extra={'clip_id': clip.id, 'error': str(e)}
                    )
                    clip.status = 'error'
                    clip.error_message = f"Download failed: {str(e)}"

        if downloaded_count > 0:
            db.session.commit()
            current_app.logger.info(
                "Downloaded clips in worker",
                extra={'count': downloaded_count, 'use_case_id': use_case_id}
            )
        
        # Check for voiceover
        self.update_state(
            state=states.STARTED,
            meta={
                'step': 'voiceover',
                'progress': 10,
                'message': 'Preparing voiceover...'
            }
        )
        
        voiceover_path = options.get('voiceover_path')
        include_voiceover = options.get('include_voiceover', True)
        
        if include_voiceover:
            if not voiceover_path:
                generator = VoiceoverGenerator(
                    api_key=current_app.config.get('ELEVENLABS_API_KEY'),
                    upload_folder=upload_folder,
                    ffmpeg_path=ffmpeg_path
                )
                voiceover_result = generator.generate_voiceover(
                    use_case=use_case,
                    script=script,
                    force=options.get('force_voiceover', False),
                    background_music=options.get('background_music')
                )
                if not voiceover_result.get('success'):
                    raise RuntimeError(voiceover_result.get('error', 'Voiceover generation failed'))
                voiceover_path = voiceover_result['file_path']
            else:
                resolved_voiceover = voiceover_path
                if not os.path.isabs(resolved_voiceover):
                    resolved_voiceover = os.path.join(upload_folder, resolved_voiceover)
                if not os.path.exists(resolved_voiceover):
                    raise FileNotFoundError(f"Voiceover file not found at {voiceover_path}")
        
        # Start assembly
        self.update_state(
            state='PROGRESS',
            meta={
                'step': 'assembly',
                'progress': 30,
                'message': 'Merging video clips...'
            }
        )
        
        assembler = SmartVideoAssembler(
            upload_folder=upload_folder,
            ffmpeg_path=ffmpeg_path
        )
        
        # Run assembly
        assembly_result = assembler.assemble_use_case_smart(
            use_case=use_case,
            script=script,
            audio_relative_path=voiceover_path,
            transition=options.get('transition', 'cut'),
            quality=options.get('quality', 'medium'),
            format_override=options.get('format_override'),
            transition_duration=float(options.get('transition_duration', 0.5))
        )
        
        if not assembly_result.get('success'):
            raise RuntimeError(assembly_result.get('error', 'Assembly failed'))
        
        final_video_data = assembly_result.get('final_video') or {}
        if not final_video_data:
            raise RuntimeError('Assembly completed without final video data')
        
        # Final processing
        self.update_state(
            state='PROGRESS',
            meta={
                'step': 'finalizing',
                'progress': 95,
                'message': 'Finalizing video...'
            }
        )
        
        # Complete
        return {
            'success': True,
            'video_path': final_video_data.get('file_path'),
            'video_url': final_video_data.get('video_url'),
            'duration': final_video_data.get('duration'),
            'file_size': final_video_data.get('file_size'),
            'final_video_id': final_video_data.get('id'),
            'final_video': final_video_data,
            'assembly_info': assembly_result.get('assembly_info')
        }
        
    except SoftTimeLimitExceeded:
        db.session.rollback()
        # Let Celery handle the failure state - don't manually set it
        raise
        
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Assembly failed")
        
        # Don't manually set FAILURE state here - let Celery handle it
        # Manual FAILURE state interferes with retry exception serialization
        
        # Retry with exponential backoff
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery.task
def cleanup_old_assembly_jobs(max_age_hours: int = 24):
    """Clean up old assembly job results from Redis.
    
    Args:
        max_age_hours: Maximum age of results to keep
    """
    # This is a maintenance task that can be run periodically
    # Implementation depends on how we store job results
    pass


@celery.task(bind=True, max_retries=3)
def generate_clip_async(
    self,
    clip_id: int,
    image_url: Optional[str] = None
) -> Dict[str, Any]:
    """Generate a single video clip asynchronously via Pollo.ai.
    
    This task handles the Pollo API call in the background,
    avoiding the 30s Render web request timeout.
    
    Args:
        self: Celery task instance
        clip_id: ID of the VideoClip to generate
        image_url: Optional image URL for image-to-video
        
    Returns:
        Dict with result info
    """
    from app.services.video_clip_manager import VideoClipManager
    from app.models import VideoClip, UseCase
    
    try:
        # Update status to show we're starting
        clip = db.session.get(VideoClip, clip_id)
        if not clip:
            raise ValueError(f"Clip {clip_id} not found")
        
        # Mark as generating
        clip.status = 'generating'
        db.session.commit()
        
        self.update_state(
            state=states.STARTED,
            meta={
                'clip_id': clip_id,
                'step': 'starting_generation',
                'message': f'Starting video generation for clip {clip_id}'
            }
        )
        
        # Get API key and create manager
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        # Start generation (this calls Pollo API)
        result = manager.start_generation(clip_id, image_url=image_url)
        
        if result.get('success'):
            self.update_state(
                state=states.SUCCESS,
                meta={
                    'clip_id': clip_id,
                    'task_id': result.get('task_id'),
                    'step': 'queued',
                    'message': 'Video generation started on Pollo.ai'
                }
            )
            return {
                'success': True,
                'clip_id': clip_id,
                'task_id': result.get('task_id'),
                'status': 'generating'
            }
        else:
            # Mark as error
            clip.status = 'error'
            clip.error_message = result.get('error', 'Unknown error')
            db.session.commit()
            
            raise RuntimeError(result.get('error', 'Generation failed'))
            
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(f"Clip generation failed for clip {clip_id}")
        
        # Retry with exponential backoff
        countdown = 30 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery.task(bind=True, max_retries=3)
def generate_clips_batch_async(
    self,
    use_case_id: int,
    clip_configs: list,
    selected_image_url: Optional[str] = None
) -> Dict[str, Any]:
    """Generate multiple clips asynchronously.
    
    Args:
        self: Celery task instance
        use_case_id: ID of the use case
        clip_configs: List of clip config dicts with prompt, clip_type, etc.
        selected_image_url: Optional image URL to use for all clips
        
    Returns:
        Dict with results for all clips
    """
    from app.services.video_clip_manager import VideoClipManager
    from app.models import VideoClip, UseCase
    
    results = []
    errors = []
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        use_case = db.session.get(UseCase, use_case_id)
        if not use_case:
            raise ValueError(f"Use case {use_case_id} not found")
        
        existing_clips = VideoClip.query.filter_by(use_case_id=use_case_id).count()
        
        for i, config in enumerate(clip_configs):
            clip_index = existing_clips + i
            
            self.update_state(
                state='PROGRESS',
                meta={
                    'step': f'generating_clip_{i+1}',
                    'progress': int((i / len(clip_configs)) * 100),
                    'message': f'Creating clip {i+1} of {len(clip_configs)}'
                }
            )
            
            try:
                # Create clip record
                clip = manager.create_clip(
                    use_case_id=use_case_id,
                    sequence_order=clip_index,
                    prompt=config['prompt'],
                    model=config.get('model_choice'),
                    generation_strategy=config.get('generation_strategy', 'composite_then_kling'),
                    asset_source=config.get('asset_source', 'product_image'),
                    script_segment_ref=config.get('script_segment', ''),
                    analysis_metadata={
                        'clip_type': config.get('clip_type'),
                        'generation_strategy': config.get('generation_strategy', 'composite_then_kling'),
                        'script_segment': config.get('script_segment', ''),
                        'storyboard_source': 'phase1_router'
                    },
                    length=5
                )
                
                # Start generation
                use_image = bool(config.get('use_image', True))
                result = manager.start_generation(
                    clip.id,
                    image_url=(config.get('image_url') or selected_image_url) if use_image else None,
                    allow_auto_image=use_image
                )
                
                if result.get('success'):
                    results.append({
                        'clip_id': clip.id,
                        'task_id': result.get('task_id'),
                        'status': 'generating'
                    })
                else:
                    errors.append({
                        'clip_index': i,
                        'error': result.get('error', 'Unknown error')
                    })
                    
            except Exception as e:
                current_app.logger.error(f"Failed to generate clip {i}: {e}")
                errors.append({
                    'clip_index': i,
                    'error': str(e)
                })
        
        return {
            'success': len(errors) == 0 or len(results) > 0,
            'clips': results,
            'errors': errors if errors else None,
            'total_requested': len(clip_configs),
            'started': len(results),
            'failed': len(errors)
        }
        
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(f"Batch generation failed for use_case {use_case_id}")
        raise


def _resolve_media_path(candidate: Optional[str], upload_root: str) -> Optional[str]:
    """Convert relative upload paths to absolute paths."""
    if not candidate:
        return None
    if os.path.isabs(candidate):
        return candidate
    return os.path.join(upload_root, candidate)


def _probe_media_duration(media_path: Optional[str], ffprobe_path: str) -> Optional[float]:
    """Return media duration using ffprobe when available."""
    if not media_path or not os.path.exists(media_path):
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                media_path
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return round(float(result.stdout.strip()), 2)
    except Exception:
        return None


def _ensure_hook_folder(upload_root: str, hook_id: int) -> str:
    folder = os.path.join(upload_root, 'hooks', str(hook_id))
    os.makedirs(folder, exist_ok=True)
    return folder


@celery.task(bind=True, max_retries=0)
def generate_hook_video(
    self,
    hook_id: int,
    image_path: Optional[str] = None,
    audio_path: Optional[str] = None,
    variant_index: Optional[int] = None,
    upload_root: Optional[str] = None,
    duration_seconds: float = 5.0,
    options: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Create a short animated hook preview with subtle motion."""
    options = options or {}
    upload_folder = os.path.abspath(upload_root or current_app.config.get('UPLOAD_FOLDER', './uploads'))
    ffmpeg_path = current_app.config.get('FFMPEG_PATH', 'ffmpeg')
    ffprobe_path = current_app.config.get('FFPROBE_PATH') or ffmpeg_path.replace('ffmpeg', 'ffprobe')

    format_key = (options.get('format') or '9:16').strip()
    dimensions = {
        '9:16': (1080, 1920),
        '16:9': (1920, 1080),
        '1:1': (1080, 1080),
        '4:5': (1080, 1350)
    }
    width, height = dimensions.get(format_key, (1080, 1920))
    fps = max(12, min(int(options.get('fps', 30)), 60))
    min_duration = max(3.0, float(duration_seconds or 5.0))

    def mark_failed(message: str) -> None:
        try:
            db.session.rollback()
        except Exception:
            pass
        hook_obj = db.session.get(Hook, hook_id)
        if not hook_obj:
            return
        hook_obj.status = 'failed'
        hook_obj.error_message = message
        db.session.commit()

    try:
        hook = db.session.get(Hook, hook_id)
        if not hook:
            raise ValueError(f'Hook {hook_id} not found')

        selected_variant = variant_index if variant_index is not None else hook.winning_variant_index
        if selected_variant is None:
            raise ValueError('Hook does not have a selected variant yet')

        variant_images = hook.image_paths or []
        variant_index_safe = int(selected_variant)
        if variant_index_safe < 0 or variant_index_safe >= len(variant_images):
            raise ValueError('Selected hook variant is missing a preview image')

        resolved_image = _resolve_media_path(image_path or variant_images[variant_index_safe], upload_folder)
        resolved_audio = _resolve_media_path(audio_path, upload_folder)
        if not resolved_image or not os.path.exists(resolved_image):
            raise FileNotFoundError('Hook preview image missing on disk')

        if not resolved_audio or not os.path.exists(resolved_audio):
            manifest_path = _resolve_media_path(hook.audio_path, upload_folder)
            relative_audio = None
            if manifest_path and os.path.exists(manifest_path):
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as handle:
                        manifest = json.load(handle)
                    relative_audio = manifest.get(str(variant_index_safe))
                except (OSError, json.JSONDecodeError):
                    relative_audio = None
            resolved_audio = _resolve_media_path(relative_audio, upload_folder)

        if not resolved_audio or not os.path.exists(resolved_audio):
            raise FileNotFoundError('Hook audio file missing. Regenerate previews and try again.')

        hook.status = 'animating'
        hook.error_message = None
        hook.video_path = None
        db.session.commit()

        self.update_state(
            state=states.STARTED,
            meta={'hook_id': hook.id, 'step': 'preparing', 'message': 'Preparing assets'}
        )

        audio_duration = _probe_media_duration(resolved_audio, ffprobe_path)
        target_duration = max(min_duration, audio_duration or 0.0)
        frame_count = max(int(target_duration * fps), fps)
        pad_seconds = max(target_duration - (audio_duration or 0.0), 0.0)

        hook_folder = _ensure_hook_folder(upload_folder, hook.id)
        output_filename = f'hook_variant_{variant_index_safe + 1}_animation.mp4'
        output_path = os.path.join(hook_folder, output_filename)

        zoom_speed = float(options.get('zoom_speed', 0.0008))
        zoom_speed = max(0.0002, min(zoom_speed, 0.0025))

        filter_parts = [
            (
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=cover,"
                f"crop={width}:{height},"
                f"zoompan=z='if(eq(on,1),1.05,min(1.12,zoom+{zoom_speed}))':d={frame_count}:"
                f"s={width}x{height},fps={fps},format=yuv420p[kv]"
            )
        ]

        audio_label = '1:a:0'
        if pad_seconds > 0.05:
            filter_parts.append(f"[1:a]apad=pad_dur={pad_seconds:.2f}[aud]")
            audio_label = '[aud]'

        filter_complex = ';'.join(filter_parts)

        self.update_state(
            state='PROGRESS',
            meta={'hook_id': hook.id, 'step': 'rendering', 'message': 'Rendering animation'}
        )

        cmd = [
            ffmpeg_path,
            '-y',
            '-loop', '1',
            '-i', resolved_image,
            '-i', resolved_audio,
            '-filter_complex', filter_complex,
            '-map', '[kv]',
            '-map', audio_label,
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-t', f"{target_duration:.2f}",
            '-c:a', 'aac',
            '-b:a', '192k',
            '-movflags', '+faststart'
        ]

        if pad_seconds > 0.05:
            cmd.append('-shortest')

        cmd.append(output_path)

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b'').decode('utf-8', errors='ignore')
            raise RuntimeError(f'Hook animation failed: {stderr[:200]}') from exc

        rel_path = os.path.relpath(output_path, upload_folder).replace('\\\\', '/')
        hook = db.session.get(Hook, hook.id)
        hook.video_path = rel_path
        hook.status = 'complete'
        hook.error_message = None
        db.session.commit()

        return {
            'success': True,
            'hook_id': hook.id,
            'video_path': hook.video_path,
            'duration': target_duration,
            'width': width,
            'height': height
        }

    except SoftTimeLimitExceeded:
        mark_failed('Hook animation timed out. Please retry.')
        raise
    except Exception as exc:
        mark_failed(str(exc))
        raise
