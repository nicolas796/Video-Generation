import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from kombu import Connection
from kombu.exceptions import OperationalError

from config import config

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()

# Celery instance (initialized with app in create_app)
celery = None

def _verify_celery_connectivity(app):
    """Ensure the Celery broker/backends are reachable to avoid runtime hangs."""
    # Support both lowercase (Celery 6.0) and uppercase (legacy) config keys
    broker_url = app.config.get('celery_broker_url') or app.config.get('CELERY_BROKER_URL')
    if not broker_url:
        raise RuntimeError('CELERY_BROKER_URL is not configured. Set REDIS_URL or CELERY_BROKER_URL.')
    connection = None
    try:
        connection = Connection(broker_url)
        connection.ensure_connection(max_retries=1, interval_start=0, interval_step=0.2, interval_max=0.5)
    except OperationalError as exc:
        raise RuntimeError(f'Unable to reach Celery broker ({broker_url}): {exc}') from exc
    finally:
        if connection:
            connection.release()
    backend_url = app.config.get('celery_result_backend') or app.config.get('CELERY_RESULT_BACKEND')
    if backend_url and backend_url != broker_url:
        backend_connection = None
        try:
            backend_connection = Connection(backend_url)
            backend_connection.ensure_connection(max_retries=1, interval_start=0, interval_step=0.2, interval_max=0.5)
        except OperationalError as exc:
            raise RuntimeError(f'Unable to reach Celery result backend ({backend_url}): {exc}') from exc
        finally:
            if backend_connection:
                backend_connection.release()

def _init_celery(app):
    """Initialize Celery and disable it cleanly if configuration is missing."""
    app.config.setdefault('CELERY_AVAILABLE', False)
    app.config.setdefault('CELERY_DISABLED_REASON', None)
    global celery
    try:
        from app.celery_app import make_celery
        celery = make_celery(app)
        _verify_celery_connectivity(app)
        app.config['CELERY_AVAILABLE'] = True
        app.config['CELERY_DISABLED_REASON'] = None
        app.logger.info("Celery initialized successfully")
    except Exception as e:
        app.logger.warning(f"Celery initialization failed (async tasks unavailable): {e}")
        app.config['CELERY_AVAILABLE'] = False
        app.config['CELERY_DISABLED_REASON'] = str(e)
        celery = None

def create_app(config_name='default'):
    """Application factory pattern."""
    # Get the absolute path to the template and static folders
    base_dir = os.path.abspath(os.path.dirname(__file__))
    template_dir = os.path.join(os.path.dirname(base_dir), 'templates')
    static_dir = os.path.join(os.path.dirname(base_dir), 'static')
    
    app = Flask(__name__, 
                template_folder=template_dir,
                static_folder=static_dir)
    
    # Load configuration
    app.config.from_object(config[config_name])
    
    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    
    # Initialize Celery with Flask app context
    _init_celery(app)
    
    # Initialize WhiteNoise for static files in production
    if app.config['FLASK_ENV'] == 'production':
        from whitenoise import WhiteNoise
        app.wsgi_app = WhiteNoise(app.wsgi_app, root=static_dir, prefix='static/')
        # Also serve uploaded files
        upload_folder = app.config['UPLOAD_FOLDER']
        if os.path.exists(upload_folder):
            app.wsgi_app.add_files(upload_folder, prefix='uploads/')
    
    # Initialize authentication
    from app.auth import init_login_manager, create_default_admin
    init_login_manager(app)
    
    # Ensure upload directories exist and create default admin
    with app.app_context():
        # Log upload folder location for debugging
        upload_folder = app.config['UPLOAD_FOLDER']
        app.logger.info(f"Upload folder: {upload_folder}")
        if os.path.exists('/var/data'):
            app.logger.info("Render Disk detected at /var/data")
        
        for folder in [app.config['PRODUCT_UPLOAD_FOLDER'], 
                       app.config['CLIP_UPLOAD_FOLDER'],
                       app.config['FINAL_UPLOAD_FOLDER']]:
            os.makedirs(folder, exist_ok=True)
            app.logger.info(f"Ensured directory exists: {folder}")
        
        # Create default admin user if configured (skip during build phase)
        # Skip if RENDER environment variable is set but database is not ready
        if not os.getenv('RENDER') or os.getenv('DATABASE_URL'):
            create_default_admin()
        
        # Create all database tables (for new models like ClipLibrary)
        # This ensures new tables are created without needing manual migrations
        try:
            db.create_all()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Could not create tables: {e}")
    
    # Register blueprints
    from app.routes import main_bp
    from app.auth import auth_bp
    from app.brand_routes import brand_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(brand_bp)

    # Register brand context middleware (loads g.current_brand on every request)
    from app.brand_context import load_brand_context
    app.before_request(load_brand_context)
    
    # Register error handlers
    register_error_handlers(app)
    
    # Register CLI commands
    from app.cli import init_cli
    init_cli(app)
    
    return app

def register_error_handlers(app):
    """Register error handlers."""
    
    @app.errorhandler(404)
    def not_found_error(error):
        return {'error': 'Not found'}, 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return {'error': 'Internal server error'}, 500


# Create app instance for gunicorn (Render auto-detects Flask and uses 'gunicorn app:app')
app = create_app()
