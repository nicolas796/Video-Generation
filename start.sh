#!/bin/bash
# Start the web service.  Assembly tasks now run in background threads
# inside the Gunicorn process, so a separate Celery worker is not needed.

exec gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --worker-class gevent \
    --workers 1 \
    --timeout 600 \
    --keep-alive 2 \
    --max-requests 500
