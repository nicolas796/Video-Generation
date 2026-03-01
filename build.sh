#!/bin/bash
# Build script for Render.com deployment
# This script runs during the build phase

set -e  # Exit on error

echo "=========================================="
echo "Building Product Video Generator..."
echo "Started at: $(date)"
echo "=========================================="

# Install Python dependencies
echo "Installing dependencies..."
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install dependencies"
    exit 1
fi
echo "✓ Dependencies installed"

# Create upload directories if they don't exist
echo "Creating upload directories..."
mkdir -p uploads/products
mkdir -p uploads/clips
mkdir -p uploads/final
echo "✓ Upload directories created"

# Database migrations
# Set RENDER_BUILD flag so config.py knows it's build time and won't raise SECRET_KEY error
echo ""
echo "=========================================="
echo "Checking database..."
echo "=========================================="
export RENDER_BUILD=true
export FLASK_APP=app

# Check if using SQLite with existing tables (skip migrations)
python -c "
import os
import sys
os.environ['RENDER_BUILD'] = 'true'
from app import create_app, db
from sqlalchemy import inspect

app = create_app()
with app.app_context():
    db_url = app.config['SQLALCHEMY_DATABASE_URI']
    print(f'Database URL type: {db_url.split(\"://\")[0] if \"://\" in db_url else \"unknown\"}')
    
    # If SQLite, check if tables exist
    if 'sqlite' in db_url:
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        print(f'SQLite tables found: {tables}')
        
        required_tables = ['products', 'use_cases', 'scripts', 'video_clips', 'final_videos', 'users', 'activity_logs']
        if all(t in tables for t in required_tables):
            print('All required tables exist. Skipping migrations.')
            sys.exit(0)  # Success, skip migrations
        else:
            print(f'Missing tables. Running migrations...')
            sys.exit(1)  # Need migrations
    else:
        print('PostgreSQL detected. Will run migrations.')
        sys.exit(1)  # Need migrations for PostgreSQL
" 2>&1

MIGRATION_CHECK=$?

if [ $MIGRATION_CHECK -eq 0 ]; then
    echo "✓ Database already set up. Skipping migrations."
else
    echo ""
    echo "Running flask db upgrade..."
    flask db upgrade 2>&1
    MIGRATION_STATUS=$?

    if [ $MIGRATION_STATUS -ne 0 ]; then
        echo ""
        echo "=========================================="
        echo "WARNING: Database migration failed!"
        echo "Exit code: $MIGRATION_STATUS"
        echo "=========================================="
        echo "This may be OK if tables already exist."
        echo "Continuing with build..."
    else
        echo ""
        echo "✓ Database migrations completed successfully!"
    fi
fi

echo ""
echo "=========================================="
echo "Build completed successfully!"
echo "Finished at: $(date)"
echo "=========================================="
