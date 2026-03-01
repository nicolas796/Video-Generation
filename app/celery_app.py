"""Celery configuration for async video processing."""
import os
from celery import Celery

# Single Celery instance shared across the app and worker processes
celery = Celery('product_video_generator')

def make_celery(app=None):
    """Configure the global Celery instance and optionally bind Flask context."""
    # Get Redis URL from environment or use local Redis
    broker_url = os.getenv(
        'CELERY_BROKER_URL',
        os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    )
    
    result_backend = os.getenv(
        'CELERY_RESULT_BACKEND',
        os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    )
    
    # Base configuration (lowercase keys for Celery 6.0 compatibility)
    celery.conf.update(
        broker_url=broker_url,
        result_backend=result_backend,
        imports=['app.tasks.video_tasks'],

        # Task serialization
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',

        # Task execution
        task_track_started=True,
        task_time_limit=1800,  # 30 minutes hard limit
        task_soft_time_limit=1500,  # 25 minutes soft limit

        # Worker settings
        worker_prefetch_multiplier=1,  # Process one task at a time
        worker_max_tasks_per_child=50,  # Restart worker after 50 tasks

        # Result storage
        result_expires=3600 * 24,  # Results expire after 24 hours
        result_extended=True,

        # Retry settings
        task_default_retry_delay=60,  # 1 minute between retries
        task_max_retries=3,

        # Broker connection (Celery 6.0 compatibility)
        broker_connection_retry_on_startup=True,

        # Visibility timeout (must be > task time limit)
        broker_transport_options={
            'visibility_timeout': 3600 * 6  # 6 hours
        }
    )
    
    # Update with Flask app config if provided
    if app:
        # Map lowercase celery config keys from Flask config
        flask_config = {
            'broker_url': app.config.get('celery_broker_url') or app.config.get('CELERY_BROKER_URL'),
            'result_backend': app.config.get('celery_result_backend') or app.config.get('CELERY_RESULT_BACKEND'),
        }
        celery.conf.update(flask_config)
        
        # Add Flask context to tasks so current_app/db work inside Celery workers
        TaskBase = celery.Task

        class ContextTask(TaskBase):
            abstract = True

            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return TaskBase.__call__(self, *args, **kwargs)
        
        celery.Task = ContextTask
    
    return celery


# Configure Celery with defaults immediately; Flask will call make_celery(app) later
make_celery()
