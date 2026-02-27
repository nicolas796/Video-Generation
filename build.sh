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

# Run database migrations
# Set RENDER_BUILD flag so config.py knows it's build time and won't raise SECRET_KEY error
echo ""
echo "=========================================="
echo "Running database migrations..."
echo "=========================================="
export RENDER_BUILD=true
export FLASK_APP=app

# Run migrations with verbose output and error handling
echo "Current migration heads:"
flask db heads || echo "Warning: Could not check migration heads"

echo ""
echo "Running flask db upgrade..."
flask db upgrade --verbose 2>&1
MIGRATION_STATUS=$?

if [ $MIGRATION_STATUS -ne 0 ]; then
    echo ""
    echo "=========================================="
    echo "ERROR: Database migration failed!"
    echo "Exit code: $MIGRATION_STATUS"
    echo "=========================================="
    
    # Try to get more diagnostic info
    echo ""
    echo "Diagnostic information:"
    echo "----------------------"
    echo "Database URL (sanitized):"
    echo "$DATABASE_URL" | sed 's/:\/\/[^:]*:[^@]*@/:\/\/****:****@/'
    
    echo ""
    echo "Migration history:"
    flask db history 2>&1 || echo "Could not get migration history"
    
    echo ""
    echo "Current database tables (if accessible):"
    python -c "
import os
os.environ['RENDER_BUILD'] = 'true'
from app import create_app, db
app = create_app()
with app.app_context():
    try:
        result = db.session.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=\\'public\\'')
        tables = [row[0] for row in result]
        print('Tables:', tables)
    except Exception as e:
        print('Error checking tables:', e)
" 2>&1 || echo "Could not check tables"
    
    exit 1
fi

echo ""
echo "✓ Database migrations completed successfully!"

# Verify tables exist
echo ""
echo "Verifying database tables..."
python -c "
import os
os.environ['RENDER_BUILD'] = 'true'
from app import create_app, db
app = create_app()
with app.app_context():
    try:
        result = db.session.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=\\'public\\'')
        tables = [row[0] for row in result]
        print('Tables in database:', tables)
        
        required_tables = ['users', 'products', 'use_cases', 'scripts', 'video_clips', 'final_videos', 'activity_logs']
        missing = [t for t in required_tables if t not in tables]
        if missing:
            print('WARNING: Missing tables:', missing)
        else:
            print('✓ All required tables present')
    except Exception as e:
        print('Warning: Could not verify tables:', e)
" 2>&1

echo ""
echo "=========================================="
echo "Build completed successfully!"
echo "Finished at: $(date)"
echo "=========================================="
