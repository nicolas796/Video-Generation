"""Celery tasks for asynchronous hook generation."""
from __future__ import annotations

from typing import Dict

from celery import states
from celery.exceptions import SoftTimeLimitExceeded
from flask import current_app

from app import db
from app.celery_app import celery
from app.models import Hook, Product, UseCase
from app.services.hook_generator import HookGenerator, build_hook_product_payload


def _mark_hook_failure(hook_id: int, message: str) -> None:
    hook = db.session.get(Hook, hook_id)
    if not hook:
        return
    hook.status = 'failed'
    hook.error_message = message
    db.session.commit()


@celery.task(bind=True, max_retries=2, default_retry_delay=30)
def generate_hook_variants(self, hook_id: int) -> Dict[str, object]:
    """Generate hook variants in the background."""

    hook = db.session.get(Hook, hook_id)
    if not hook:
        current_app.logger.warning('Hook not found for async generation', extra={'hook_id': hook_id})
        return {'success': False, 'error': 'Hook not found', 'hook_id': hook_id}

    use_case = db.session.get(UseCase, hook.use_case_id)
    product = db.session.get(Product, use_case.product_id) if use_case else None
    if not use_case or not product:
        _mark_hook_failure(hook_id, 'Use case or product missing for hook generation.')
        return {'success': False, 'error': 'Use case or product missing.', 'hook_id': hook_id}

    generator = HookGenerator(api_key=current_app.config.get('OPENAI_API_KEY'))
    payload = build_hook_product_payload(product, use_case)

    try:
        self.update_state(state=states.STARTED, meta={'hook_id': hook_id, 'message': 'Generating hook variants'})
        variants = generator.generate_variants(payload, hook.hook_type, count=3)
        hook.variants = variants
        hook.status = 'draft'
        hook.error_message = None
        db.session.commit()
        current_app.logger.info('Hook variants generated', extra={'hook_id': hook_id, 'count': len(variants)})
        return {'success': True, 'hook_id': hook_id, 'variant_count': len(variants)}
    except SoftTimeLimitExceeded:
        db.session.rollback()
        _mark_hook_failure(hook_id, 'Hook generation timed out.')
        raise
    except Exception as exc:
        db.session.rollback()
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
        _mark_hook_failure(hook_id, f'Hook generation failed: {exc}')
        raise
