"""In-process background task runner using threads.

Replaces Celery for single-service deployments where running a separate
worker process is unreliable (e.g. Render web services).
"""
import logging
import os
import threading
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Thread-safe task state store
_tasks: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Get current state of a task."""
    with _lock:
        return _tasks.get(task_id, {}).copy() if task_id in _tasks else None


def _set_task(task_id: str, state: Dict[str, Any]):
    with _lock:
        _tasks[task_id] = state


class TaskContext:
    """Passed to the task function so it can update progress."""

    def __init__(self, task_id: str):
        self.task_id = task_id

    def update_state(self, status: str, progress: int = 0,
                     message: str = '', step: str = '', **extra):
        state = {
            'task_id': self.task_id,
            'status': status,
            'progress': progress,
            'message': message,
            'step': step,
            'success': None,
            **extra,
        }
        _set_task(self.task_id, state)


def submit(fn: Callable, *args, **kwargs) -> str:
    """Run *fn* in a background thread and return a task_id.

    ``fn`` receives a ``TaskContext`` as its first argument so it can
    report progress.  Its return value is stored as the task result.
    """
    task_id = str(uuid.uuid4())
    _set_task(task_id, {
        'task_id': task_id,
        'status': 'PENDING',
        'progress': 0,
        'message': 'Task queued...',
        'success': None,
    })

    def _wrapper():
        ctx = TaskContext(task_id)
        try:
            result = fn(ctx, *args, **kwargs)
            _set_task(task_id, {
                'task_id': task_id,
                'status': 'SUCCESS',
                'progress': 100,
                'message': 'Complete',
                'success': True,
                'result': result,
            })
        except Exception as exc:
            logger.exception("Background task %s failed", task_id)
            _set_task(task_id, {
                'task_id': task_id,
                'status': 'FAILURE',
                'progress': 0,
                'message': str(exc),
                'success': False,
                'error': str(exc),
            })

    t = threading.Thread(target=_wrapper, daemon=True, name=f"task-{task_id[:8]}")
    t.start()
    return task_id
