import os
import re
import secrets
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def get_secret_key():
    """Get SECRET_KEY from environment or generate one for development only."""
    secret_key = os.getenv('SECRET_KEY')
    
    if secret_key:
        return secret_key
    
    # Check if we're in a build phase (Render build or similar)
    # During build, we don't need a real SECRET_KEY for migrations
    if os.getenv('RENDER_BUILD') == 'true' or os.getenv('BUILD_PHASE') == 'true':
        return 'build-temp-key-not-for-production'
    
    # In production, SECRET_KEY must be set
    if os.getenv('FLASK_ENV') == 'production':
        raise RuntimeError(
            "CRITICAL: SECRET_KEY environment variable is not set! "
            "Please set a strong, random SECRET_KEY for production."
        )
    
    # For development only: generate a random key (sessions won't persist across restarts)
    return secrets.token_hex(32)

def get_database_url():
    """
    Get database URL from environment.
    
    Render provides DATABASE_URL with 'postgres://' prefix,
    but SQLAlchemy requires 'postgresql://'. This function handles the conversion.
    """
    database_url = os.getenv('DATABASE_URL')
    
    if not database_url:
        # Default to SQLite for local development
        return 'sqlite:///app.db'
    
    # Fix for Render's postgres:// vs postgresql://
    # Render uses postgres:// but SQLAlchemy requires postgresql://
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    
    return database_url

def get_upload_folder():
    """Get upload folder path.
    
    In production on Render, use the persistent disk if available.
    Falls back to local uploads folder.
    """
    # Check for Render Disk (persistent storage)
    render_disk = os.getenv('RENDER_DISK_MOUNT_PATH')
    if render_disk and os.path.exists(render_disk):
        return os.path.join(render_disk, 'uploads')
    
    # Check for explicit UPLOAD_FOLDER env var
    upload_folder = os.getenv('UPLOAD_FOLDER')
    if upload_folder:
        return upload_folder
    
    # Default to local uploads folder
    base_dir = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_dir, 'uploads')


class Config:
    """Base configuration class."""
    
    # Flask
    FLASK_ENV = os.getenv('FLASK_ENV', 'development')
    FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    SECRET_KEY = get_secret_key()
    
    # Database
    SQLALCHEMY_DATABASE_URI = get_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_recycle': 299,  # Recycle connections before Render's 5-min timeout
        'pool_pre_ping': True,  # Verify connections before use
    }
    
    # Upload paths
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    UPLOAD_FOLDER = get_upload_folder()
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    
    # Product uploads
    PRODUCT_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'products')
    CLIP_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'clips')
    FINAL_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'final')
    
    # API Keys
    POLLO_API_KEY = os.getenv('POLLO_API_KEY', '')
    ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '')
    MOONSHOT_API_KEY = os.getenv('MOONSHOT_API_KEY', '')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

    # Webhook + external access configuration
    POLLO_WEBHOOK_SECRET = os.getenv('POLLO_WEBHOOK_SECRET', '')
    APP_BASE_URL = (
        os.getenv('APP_BASE_URL')
        or os.getenv('PUBLIC_BASE_URL')
        or os.getenv('EXTERNAL_BASE_URL')
        or 'http://localhost:5000'
    )
    
    # ElevenLabs default voice
    DEFAULT_VOICE_ID = os.getenv('DEFAULT_VOICE_ID', 'XB0fDUnXU5powFXDhCwa')

class DevelopmentConfig(Config):
    """Development configuration."""
    FLASK_ENV = 'development'
    FLASK_DEBUG = True

class ProductionConfig(Config):
    """Production configuration."""
    FLASK_ENV = 'production'
    FLASK_DEBUG = False
    
    # Additional production settings
    SESSION_COOKIE_SECURE = True  # Only send cookies over HTTPS
    SESSION_COOKIE_HTTPONLY = True  # Prevent XSS access to cookies
    SESSION_COOKIE_SAMESITE = 'Lax'  # CSRF protection
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour session timeout

class TestingConfig(Config):
    """Testing configuration."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'

# Configuration dictionary
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
