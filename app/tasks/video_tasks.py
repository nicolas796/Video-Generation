"""Async video assembly tasks using Celery."""
import os
import time
from typing import Dict, Any, Optional
from celery import states
from celery.exceptions import SoftTimeLimitExceeded

from app import db
from app.models import UseCase, Script, FinalVideo
from app.celery_app import celery


@celery.task(bind=True, max_retries=3)
def assemble_final_video_async(
    self,
    use_case_id: int,
    script_id: int,
    options: Dict[str, Any]
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
    from flask import current_app
    from app.services.smart_assembly import SmartVideoAssembler
    from app.services.voiceover import VoiceoverGenerator
    
    upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
    ffmpeg_path = current_app.config.get('FFMPEG_PATH', 'ffmpeg')
    
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
        use_case = UseCase.query.get(use_case_id)
        script = Script.query.get(script_id)
        
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
        
        if include_voiceover and not voiceover_path:
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
            if voiceover_result.get('success'):
                voiceover_path = voiceover_result['file_path']
        
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
        
        # Update progress during assembly
        def progress_callback(step: str, progress: int):
            self.update_state(
                state='PROGRESS',
                meta={
                    'step': step,
                    'progress': 30 + (progress * 0.6),  # 30-90% range
                    'message': f'Processing: {step}...'
                }
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
            raise Exception(assembly_result.get('error', 'Assembly failed'))
        
        # Final processing
        self.update_state(
            state='PROGRESS',
            meta={
                'step': 'finalizing',
                'progress': 95,
                'message': 'Finalizing video...'
            }
        )
        
        # Create FinalVideo record
        final_video = FinalVideo(
            use_case_id=use_case_id,
            file_path=assembly_result['video_path'],
            duration=assembly_result.get('duration', 0),
            status='complete'
        )
        db.session.add(final_video)
        db.session.commit()
        
        # Complete
        return {
            'success': True,
            'video_path': assembly_result['video_path'],
            'duration': assembly_result.get('duration', 0),
            'file_size': assembly_result.get('file_size', 0),
            'final_video_id': final_video.id
        }
        
    except SoftTimeLimitExceeded:
        self.update_state(
            state=states.FAILURE,
            meta={
                'step': 'timeout',
                'progress': 0,
                'message': 'Assembly timed out (30 min limit)'
            }
        )
        raise
        
    except Exception as exc:
        # Log error and retry
        current_app.logger.error(f"Assembly failed: {exc}")
        
        # Update state to show error
        self.update_state(
            state=states.FAILURE,
            meta={
                'step': 'error',
                'progress': 0,
                'message': f'Error: {str(exc)}'
            }
        )
        
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
