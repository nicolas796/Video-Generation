"""CLI commands for the Product Video Generator."""
import click
from flask.cli import with_appcontext

from app import db
from app.models import User
from app.auth import create_admin_user


@click.command('create-admin')
@click.option('--username', '-u', required=True, help='Username for the admin user')
@click.option('--password', '-p', required=True, help='Password for the admin user')
@click.option('--admin', '-a', is_flag=True, default=True, help='Grant admin privileges (default: True)')
@with_appcontext
def create_admin_command(username, password, admin):
    """Create a new admin user."""
    success, message, user = create_admin_user(username, password, is_admin=admin)
    
    if success:
        click.echo(click.style(f'✓ {message}', fg='green'))
        click.echo(f'  Username: {user.username}')
        click.echo(f'  Admin: {user.is_admin}')
    else:
        click.echo(click.style(f'✗ {message}', fg='red'))
        raise click.Abort()


@click.command('list-users')
@with_appcontext
def list_users_command():
    """List all users in the database."""
    users = User.query.all()
    
    if not users:
        click.echo('No users found.')
        return
    
    click.echo(f'Found {len(users)} user(s):')
    click.echo('')
    click.echo(f'{"ID":<5} {"Username":<20} {"Admin":<8} {"Last Login":<20}')
    click.echo('-' * 60)
    
    for user in users:
        last_login = user.last_login.strftime('%Y-%m-%d %H:%M') if user.last_login else 'Never'
        click.echo(f'{user.id:<5} {user.username:<20} {str(user.is_admin):<8} {last_login:<20}')


@click.command('reset-password')
@click.option('--username', '-u', required=True, help='Username of the user')
@click.option('--password', '-p', required=True, help='New password')
@with_appcontext
def reset_password_command(username, password):
    """Reset a user's password."""
    user = User.query.filter_by(username=username).first()
    
    if not user:
        click.echo(click.style(f'✗ User "{username}" not found', fg='red'))
        raise click.Abort()
    
    if len(password) < 8:
        click.echo(click.style('✗ Password must be at least 8 characters', fg='red'))
        raise click.Abort()
    
    user.set_password(password)
    db.session.commit()
    click.echo(click.style(f'✓ Password reset for user "{username}"', fg='green'))


def init_cli(app):
    """Register CLI commands with the Flask app."""
    app.cli.add_command(create_admin_command)
    app.cli.add_command(list_users_command)
    app.cli.add_command(reset_password_command)
