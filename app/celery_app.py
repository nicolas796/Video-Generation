"""Celery configuration for async video processing."""
import os
from celery import Celery

def make_celery(app=None):
    """Create and configure Celery app.
    
    Args:
        app: Optional Flask app for context
        
    Returns:
        Configured Celery instance
    """
    # Get Redis URL from environment or use local Redis
    broker_url = os.getenv(
        'CELERY_BROKER_URL',
        os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    )
    
    result_backend = os.getenv(
        'CELERY_RESULT_BACKEND',
        os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    )
    
    celery = Celery(
        'product_video_generator',
        broker=broker_url,
        backend=result_backend,
        include=['app.tasks.video_tasks']
    )
    
    # Celery configuration
    celery.conf.update(
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
        
        # Visibility timeout (must be > task time limit)
        broker_transport_options={
            'visibility_timeout': 3600 * 6  # 6 hours
        }
    )
    
    # Update with Flask app config if provided
    if app:
        celery.conf.update(app.config)
        
        # Add Flask context to tasks
        class ContextTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)
        
        celery.Task = ContextTask
    
    return celery


# Create the Celery instance (will be initialized with Flask app in __init__.py)
celery = make_celery()
