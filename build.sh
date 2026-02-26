#!/bin/bash
# Build script for Render.com deployment
# This script runs during the build phase

set -e  # Exit on error

echo "=========================================="
echo "Building Product Video Generator..."
echo "=========================================="

# Install Python dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Create upload directories if they don't exist
echo "Creating upload directories..."
mkdir -p uploads/products
mkdir -p uploads/clips
mkdir -p uploads/final

# Run database migrations
# Set a dummy SECRET_KEY for build-time migrations if not provided
echo "Running database migrations..."
SECRET_KEY=${SECRET_KEY:-build-temp-key} flask db upgrade

echo "=========================================="
echo "Build completed successfully!"
echo "=========================================="
