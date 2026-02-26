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
echo "Running database migrations..."
flask db upgrade

echo "=========================================="
echo "Build completed successfully!"
echo "=========================================="
