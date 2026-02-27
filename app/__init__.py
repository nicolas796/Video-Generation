from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from config import config

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()

def create_app(config_name='default'):
    """Application factory pattern."""
    import os
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
        import os
        for folder in [app.config['PRODUCT_UPLOAD_FOLDER'], 
                       app.config['CLIP_UPLOAD_FOLDER'],
                       app.config['FINAL_UPLOAD_FOLDER']]:
            os.makedirs(folder, exist_ok=True)
        
        # Create default admin user if configured (skip during build phase)
        # Skip if RENDER environment variable is set but database is not ready
        if not os.getenv('RENDER') or os.getenv('DATABASE_URL'):
            create_default_admin()
    
    # Register blueprints
    from app.routes import main_bp
    from app.auth import auth_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    
    # Register error handlers
    register_error_handlers(app)
    
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
