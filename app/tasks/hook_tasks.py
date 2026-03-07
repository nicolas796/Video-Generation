"""In-process background tasks for hook generation."""
from __future__ import annotations

import logging
from typing import Dict, Optional

from flask import current_app

from app import db
from app.models import Hook, Product, UseCase
from app.services.hook_generator import HookGenerator, build_hook_product_payload
from app.tasks import thread_runner

logger = logging.getLogger(__name__)


def _mark_hook_failure(hook_id: int, message: str) -> None:
    hook = db.session.get(Hook, hook_id)
    if not hook:
        return
    hook.status = 'failed'
    hook.error_message = message
    db.session.commit()


def _generate_hook_variants(flask_app, hook_id: int, ctx: Optional[thread_runner.TaskContext] = None) -> Dict[str, object]:
    """Run hook generation inside the Flask app context."""
    with flask_app.app_context():
        hook = db.session.get(Hook, hook_id)
        if not hook:
            logger.warning('Hook not found for hook generation', extra={'hook_id': hook_id})
            return {'success': False, 'error': 'Hook not found', 'hook_id': hook_id}

        use_case = db.session.get(UseCase, hook.use_case_id)
        product = db.session.get(Product, use_case.product_id) if use_case else None
        if not use_case or not product:
            _mark_hook_failure(hook_id, 'Use case or product missing for hook generation.')
            return {'success': False, 'error': 'Use case or product missing.', 'hook_id': hook_id}

        generator = HookGenerator(api_key=current_app.config.get('OPENAI_API_KEY'))
        payload = build_hook_product_payload(product, use_case)

        try:
            if ctx:
                ctx.update_state(status='STARTED', progress=5, message='Generating hook variants', hook_id=hook_id)
            variants = generator.generate_variants(payload, hook.hook_type, count=3)
            hook.variants = variants
            hook.status = 'draft'
            hook.error_message = None
            db.session.commit()
            if ctx:
                ctx.update_state(status='SUCCESS', progress=100, message='Hook variants ready', hook_id=hook_id)
            logger.info('Hook variants generated', extra={'hook_id': hook_id, 'count': len(variants)})
            return {'success': True, 'hook_id': hook_id, 'variant_count': len(variants)}
        except Exception as exc:
            db.session.rollback()
            _mark_hook_failure(hook_id, f'Hook generation failed: {exc}')
            logger.exception('Hook generation failed for hook %s', hook_id)
            raise
        finally:
            db.session.remove()


def queue_hook_generation(hook_id: int) -> Optional[str]:
    """Submit hook generation to the in-process thread runner."""
    flask_app = current_app._get_current_object()
    try:
        task_id = thread_runner.submit(_worker_wrapper, flask_app, hook_id)
        return task_id
    except Exception as exc:
        logger.exception('Unable to queue hook generation task for hook %s', hook_id)
        _mark_hook_failure(hook_id, f'Failed to queue hook generation: {exc}')
        return None


def _worker_wrapper(ctx: thread_runner.TaskContext, flask_app, hook_id: int) -> Dict[str, object]:
    return _generate_hook_variants(flask_app, hook_id, ctx)


def run_hook_generation_blocking(hook_id: int) -> Dict[str, object]:
    """Fallback synchronous execution used if background queueing fails."""
    flask_app = current_app._get_current_object()
    return _generate_hook_variants(flask_app, hook_id, ctx=None)
