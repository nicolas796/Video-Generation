"""Authentication routes and utilities for the Product Video Generator."""
import os
from functools import wraps

from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash

from app import db
from app.models import User

# Initialize blueprint
auth_bp = Blueprint('auth', __name__)

# Initialize login manager
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'warning'


def init_login_manager(app):
    """Initialize the login manager with the app."""
    login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    """Load user by ID for Flask-Login."""
    return User.query.get(int(user_id))


def create_default_admin():
    """Create default admin user from environment variables if it doesn't exist.
    
    This function is called during app startup. It catches ALL database-related
    exceptions to prevent the app from crashing if the database isn't ready yet.
    """
    import sqlalchemy.exc
    
    admin_username = os.getenv('ADMIN_USERNAME')
    admin_password = os.getenv('ADMIN_PASSWORD')
    
    # Only create if both env vars are set
    if not admin_username or not admin_password:
        current_app.logger.debug('Skipping admin creation - ADMIN_USERNAME or ADMIN_PASSWORD not set')
        return
    
    try:
        # Check if admin already exists
        existing = User.query.filter_by(username=admin_username).first()
        if existing:
            current_app.logger.debug(f'Admin user already exists: {admin_username}')
            return
        
        # Create new admin user
        admin = User(
            username=admin_username,
            is_admin=True
        )
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        current_app.logger.info(f'Default admin user created: {admin_username}')
    except sqlalchemy.exc.ProgrammingError as e:
        # Table doesn't exist yet (migrations haven't run) - this is expected on first deploy
        current_app.logger.warning(f'Cannot create admin user - users table does not exist (migrations needed): {e}')
        db.session.rollback()
        return
    except sqlalchemy.exc.OperationalError as e:
        # Database connection issues or other operational errors
        current_app.logger.warning(f'Cannot create admin user - database not ready: {e}')
        db.session.rollback()
        return
    except Exception as e:
        # Catch ALL other exceptions to prevent startup crashes
        current_app.logger.error(f'Unexpected error creating admin user: {type(e).__name__}: {e}')
        db.session.rollback()
        return


def create_admin_user(username, password, is_admin=True):
    """Create an admin/user account manually.
    
    Args:
        username: The username for the new user
        password: The password for the new user
        is_admin: Whether the user should have admin privileges (default: True)
    
    Returns:
        Tuple of (success: bool, message: str, user: User or None)
    """
    try:
        # Check if user already exists
        existing = User.query.filter_by(username=username).first()
        if existing:
            return False, f'User "{username}" already exists', existing
        
        # Create new user
        user = User(
            username=username,
            is_admin=is_admin
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        return True, f'User "{username}" created successfully', user
    except Exception as e:
        db.session.rollback()
        return False, f'Error creating user: {type(e).__name__}: {e}', None


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Handle user login."""
    # Redirect if already logged in
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember', False)
        
        # Validate input
        if not username or not password:
            flash('Username and password are required.', 'danger')
            return render_template('login.html'), 400
        
        # Find user
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user, remember=remember)
            user.last_login = db.func.now()
            db.session.commit()
            
            # Redirect to next page or index
            next_page = request.args.get('next')
            # Security: only allow relative URLs (prevent open redirect)
            if next_page and not next_page.startswith('/'):
                next_page = None
            
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(next_page or url_for('main.index'))
        else:
            flash('Invalid username or password.', 'danger')
            return render_template('login.html'), 401
    
    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    """Handle user logout."""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    """Allow users to change their password."""
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')
    
    # Validate input
    if not current_password or not new_password or not confirm_password:
        flash('All password fields are required.', 'danger')
        return redirect(url_for('main.index'))
    
    if new_password != confirm_password:
        flash('New passwords do not match.', 'danger')
        return redirect(url_for('main.index'))
    
    if len(new_password) < 8:
        flash('New password must be at least 8 characters long.', 'danger')
        return redirect(url_for('main.index'))
    
    # Verify current password
    if not current_user.check_password(current_password):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('main.index'))
    
    # Update password
    current_user.set_password(new_password)
    db.session.commit()
    flash('Password changed successfully.', 'success')
    return redirect(url_for('main.index'))
