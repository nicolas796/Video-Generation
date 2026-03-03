"""Async video assembly tasks using Celery."""
import os
from typing import Any, Dict, Optional

from celery import states
from celery.exceptions import SoftTimeLimitExceeded
from flask import current_app

from app import db
from app.models import UseCase, Script
from app.celery_app import celery


@celery.task(bind=True, max_retries=3)
def assemble_final_video_async(
    self,
    use_case_id: int,
    script_id: int,
    options: Optional[Dict[str, Any]] = None
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
    
    Returns:
        Dict with result info
    """
    from app.services.smart_assembly import SmartVideoAssembler
    from app.services.voiceover import VoiceoverGenerator
    
    options = options or {}
    upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
    ffmpeg_path = current_app.config.get('FFMPEG_PATH', 'ffmpeg')
    current_app.logger.info('Assembly task upload_folder=%s, exists=%s', upload_folder, os.path.exists(upload_folder))
    
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
                    length=5
                )
                
                # Start generation
                result = manager.start_generation(clip.id, image_url=selected_image_url)
                
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
