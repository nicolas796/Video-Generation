import os
from flask_migrate import Migrate
from app import create_app, db
from app.models import Product, UseCase, Script, VideoClip, FinalVideo, ActivityLog, User, Hook

app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {
        'db': db,
        'Product': Product,
        'UseCase': UseCase,
        'Script': Script,
        'VideoClip': VideoClip,
        'FinalVideo': FinalVideo,
        'ActivityLog': ActivityLog,
        'User': User,
        'Hook': Hook
    }

if __name__ == '__main__':
    # Debug mode is controlled via FLASK_DEBUG environment variable
    # Default is False for security
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    port = int(os.getenv('FLASK_PORT', 5000))
    host = os.getenv('FLASK_HOST', '0.0.0.0')
    app.run(debug=debug_mode, host=host, port=port)
