#!/bin/bash
# Run both Celery worker and Gunicorn in a single service so they share
# the same Render disk at /var/data (Render disks are per-service).

# Start Celery worker in the background
celery -A app.celery_app.celery worker --loglevel=info --concurrency=1 --pool=solo &

# Start Gunicorn in the foreground (PID 1 — Render monitors this)
exec gunicorn app:app --bind 0.0.0.0:$PORT --worker-class gevent --workers 1 --timeout 600 --keep-alive 2 --max-requests 500
