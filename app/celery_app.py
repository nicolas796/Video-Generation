"""Celery configuration for async video processing."""
import os
from celery import Celery

# Get Redis URL - check environment first, then hardcoded fallback for Render
render_redis_url = 'redis://red-d6hqhb1drdic73cq0pn0:6379/0'

# Debug: Log what we're seeing
import sys
print(f"DEBUG CELERY: CELERY_BROKER_URL={os.getenv('CELERY_BROKER_URL')}", file=sys.stderr)
print(f"DEBUG CELERY: REDIS_URL={os.getenv('REDIS_URL')}", file=sys.stderr)

broker_url = os.getenv('CELERY_BROKER_URL') or os.getenv('REDIS_URL') or render_redis_url
result_backend = os.getenv('CELERY_RESULT_BACKEND') or os.getenv('REDIS_URL') or render_redis_url

print(f"DEBUG CELERY: Using broker={broker_url}", file=sys.stderr)
print(f"DEBUG CELERY: Using backend={result_backend}", file=sys.stderr)

# Create and configure Celery instance
celery = Celery(
    'product_video_generator',
    broker=broker_url,
    backend=result_backend,
    include=['app.tasks.video_tasks']
)

# Additional configuration
celery.conf.update(
    # Task serialization - use pickle for results to handle exceptions properly
    task_serializer='json',
    accept_content=['json', 'pickle'],
    result_serializer='pickle',
    
    # Task execution
    task_track_started=True,
    task_time_limit=1800,  # 30 minutes hard limit
    task_soft_time_limit=1500,  # 25 minutes soft limit
    
    # Worker settings
    worker_prefetch_multiplier=1,  # Process one task at a time
    worker_max_tasks_per_child=0,  # Disabled — worker runs as background process in combined service
    
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

def make_celery(app=None):
    """Bind Flask app context to Celery tasks."""
    if app:
        # Update with Flask app config if provided
        flask_broker = app.config.get('celery_broker_url') or app.config.get('CELERY_BROKER_URL')
        flask_backend = app.config.get('celery_result_backend') or app.config.get('CELERY_RESULT_BACKEND')
        
        if flask_broker:
            celery.conf.broker_url = flask_broker
        if flask_backend:
            celery.conf.result_backend = flask_backend
        
        # Add Flask context to tasks so current_app/db work inside Celery workers
        TaskBase = celery.Task

        class ContextTask(TaskBase):
            abstract = True

            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return TaskBase.__call__(self, *args, **kwargs)
        
        celery.Task = ContextTask
    
    return celery
