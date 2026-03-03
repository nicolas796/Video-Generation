#!/bin/bash
# Run both Celery worker and Gunicorn in a single service so they share
# the same Render disk at /var/data (Render disks are per-service).
#
# We avoid 'exec gunicorn' so the shell stays alive as PID 1 and can
# manage both child processes (forward signals, restart Celery if it dies).

set -o pipefail

echo "[start.sh] Starting services at $(date)"
echo "[start.sh] REDIS_URL set: $([ -n \"$REDIS_URL\" ] && echo yes || echo NO)"

# ── Helper: restart Celery in a loop ────────────────────────────────
start_celery() {
    while true; do
        echo "[start.sh] Starting Celery worker..."
        celery -A app.celery_app.celery worker \
            --loglevel=info \
            --concurrency=1 \
            --pool=solo 2>&1 &
        CELERY_PID=$!
        echo "[start.sh] Celery worker PID: $CELERY_PID"

        # Wait for the celery process to exit
        wait $CELERY_PID
        EXIT_CODE=$?

        # If we're shutting down (SIGTERM received), don't restart
        if [ "$SHUTTING_DOWN" = "1" ]; then
            echo "[start.sh] Celery exited during shutdown (code $EXIT_CODE)"
            break
        fi

        echo "[start.sh] WARNING: Celery worker exited with code $EXIT_CODE — restarting in 5s..."
        sleep 5
    done
}

# ── Signal handling ─────────────────────────────────────────────────
SHUTTING_DOWN=0

cleanup() {
    SHUTTING_DOWN=1
    echo "[start.sh] Received shutdown signal, stopping services..."
    # Kill child processes gracefully
    kill $CELERY_LOOP_PID 2>/dev/null
    kill $CELERY_PID 2>/dev/null
    kill $GUNICORN_PID 2>/dev/null
    wait 2>/dev/null
    echo "[start.sh] All services stopped"
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── Start Celery (auto-restarts) ────────────────────────────────────
start_celery &
CELERY_LOOP_PID=$!

# Brief pause so Celery can claim the broker connection before Gunicorn
# also initializes Celery during Flask app creation.
sleep 2

# ── Start Gunicorn ──────────────────────────────────────────────────
echo "[start.sh] Starting Gunicorn on port $PORT..."
gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --worker-class gevent \
    --workers 1 \
    --timeout 600 \
    --keep-alive 2 \
    --max-requests 500 &
GUNICORN_PID=$!
echo "[start.sh] Gunicorn PID: $GUNICORN_PID"

# ── Wait for either to exit ─────────────────────────────────────────
# If Gunicorn dies the service is down, so exit (Render will restart us).
wait $GUNICORN_PID
echo "[start.sh] Gunicorn exited — shutting down"
cleanup
