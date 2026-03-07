#!/bin/bash
# Start the web service.  Assembly tasks now run in background threads
# inside the Gunicorn process, so a separate Celery worker is not needed.

# Run pending database migrations before starting the app.
# The build phase may not have DB access on Render, so we handle it here.
echo "Running database migrations..."
export FLASK_APP=app
flask db upgrade 2>&1 || echo "WARNING: Migrations failed at startup"

# Ensure critical schema changes exist (idempotent fallback if migrations fail)
echo "Ensuring schema is up to date..."
python -c "
from app import app, db
with app.app_context():
    db.session.execute(db.text('ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)'))
    db.session.execute(db.text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)'))
    db.session.execute(db.text('''CREATE TABLE IF NOT EXISTS brand_invitations (
        id SERIAL PRIMARY KEY,
        brand_id INTEGER NOT NULL REFERENCES brands(id),
        email VARCHAR(255) NOT NULL,
        role VARCHAR(50) DEFAULT \\'member\\',
        token VARCHAR(128) UNIQUE NOT NULL,
        invited_by_id INTEGER NOT NULL REFERENCES users(id),
        status VARCHAR(50) DEFAULT \\'pending\\',
        created_at TIMESTAMP DEFAULT NOW(),
        expires_at TIMESTAMP,
        accepted_at TIMESTAMP
    )'''))
    db.session.execute(db.text('CREATE UNIQUE INDEX IF NOT EXISTS ix_brand_invitations_token ON brand_invitations (token)'))
    db.session.execute(db.text('CREATE INDEX IF NOT EXISTS ix_brand_invitations_email_brand ON brand_invitations (email, brand_id)'))
    db.session.commit()
    print('Schema verified.')
" 2>&1 || echo "WARNING: Schema fallback failed"

exec gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --worker-class gevent \
    --workers 1 \
    --timeout 600 \
    --keep-alive 2 \
    --max-requests 500
