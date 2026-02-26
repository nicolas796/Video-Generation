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
        
        # Create default admin user if configured
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
