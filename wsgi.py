"""
WSGI entry point for Gunicorn.
Usage: gunicorn wsgi:app
"""
from app import create_app
import os

# Get config from environment
config_name = os.getenv('FLASK_ENV', 'production')
app = create_app(config_name)

if __name__ == "__main__":
    app.run()
