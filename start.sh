#!/bin/bash
# Start the web service.  Assembly tasks now run in background threads
# inside the Gunicorn process, so a separate Celery worker is not needed.

# Run pending database migrations before starting the app.
# The build phase may not have DB access on Render, so we handle it here.
echo "Running database migrations..."
export FLASK_APP=app
flask db upgrade 2>&1 || echo "WARNING: Migrations failed at startup"

exec gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --worker-class gevent \
    --workers 1 \
    --timeout 600 \
    --keep-alive 2 \
    --max-requests 500
