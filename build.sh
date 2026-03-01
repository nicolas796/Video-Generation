#!/bin/bash
# Build script for Render.com deployment
# This script runs during the build phase

set -e  # Exit on error

echo "=========================================="
echo "Building Product Video Generator..."
echo "Started at: $(date)"
echo "=========================================="

# Clear Python cache to ensure fresh code
echo "Clearing Python cache..."
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
echo "✓ Cache cleared"

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
echo ""
echo "=========================================="
echo "Checking database..."
echo "=========================================="
export RENDER_BUILD=true
export FLASK_APP=app

# Show what DATABASE_URL looks like (sanitized)
echo "Database URL type: ${DATABASE_URL%%://*}"

# For Render builds, migrations often fail due to network/db not ready
# We'll make migrations optional during build and handle them at runtime
echo ""
echo "Attempting migrations (errors are OK during build)..."

# Run migrations but don't fail the build if they fail
# The app will handle migrations at startup if needed
flask db upgrade 2>&1 || echo "WARNING: Migrations failed during build. This is often OK - migrations will run at app startup."

echo ""
echo "=========================================="
echo "Build completed successfully!"
echo "Finished at: $(date)"
echo "=========================================="
