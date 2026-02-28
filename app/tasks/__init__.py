"""Celery tasks for async video processing."""
from app.celery_app import celery

__all__ = ['celery']
