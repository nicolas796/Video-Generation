"""CLI commands for the Product Video Generator."""
import os
import re
import click
from flask.cli import with_appcontext

from app import db
from app.models import User, VideoClip
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


@click.command('migrate-clip-filenames')
@with_appcontext
def migrate_clip_filenames_command():
    """Migrate clip filenames from old format to new format.
    
    Old format: clip_{id}.mp4 (e.g., clip_13.mp4)
    New format: clip_{id:03d}_{sequence_order:02d}.mp4 (e.g., clip_013_01.mp4)
    """
    from flask import current_app
    
    upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
    clips_folder = os.path.join(upload_folder, 'clips')
    
    if not os.path.exists(clips_folder):
        click.echo(click.style(f'✗ Clips folder not found: {clips_folder}', fg='red'))
        raise click.Abort()
    
    click.echo(click.style('Starting clip filename migration...', fg='blue'))
    click.echo(f'Upload folder: {upload_folder}')
    click.echo('')
    
    clips = VideoClip.query.all()
    click.echo(f'Found {len(clips)} clips in database')
    click.echo('')
    
    migrated = 0
    skipped = 0
    errors = 0
    
    for clip in clips:
        if not clip.file_path:
            click.echo(f'  [SKIP] Clip {clip.id}: No file_path')
            skipped += 1
            continue
        
        # Check if already in new format
        if re.match(r'clip_\d{3}_\d{2}\.mp4$', os.path.basename(clip.file_path)):
            click.echo(f'  [SKIP] Clip {clip.id}: Already in new format')
            skipped += 1
            continue
        
        # Old format: clip_{id}.mp4
        old_filename = f"clip_{clip.id}.mp4"
        old_path = os.path.join(clips_folder, str(clip.use_case_id), old_filename)
        
        # New format: clip_{id:03d}_{sequence_order:02d}.mp4
        new_filename = f"clip_{clip.id:03d}_{clip.sequence_order:02d}.mp4"
        new_path = os.path.join(clips_folder, str(clip.use_case_id), new_filename)
        new_db_path = f"clips/{clip.use_case_id}/{new_filename}"
        
        # Check if old file exists
        if not os.path.exists(old_path):
            current_full_path = os.path.join(upload_folder, clip.file_path)
            if os.path.exists(current_full_path):
                old_path = current_full_path
            else:
                click.echo(click.style(f'  [ERROR] Clip {clip.id}: File not found', fg='red'))
                errors += 1
                continue
        
        try:
            os.rename(old_path, new_path)
            clip.file_path = new_db_path
            click.echo(click.style(f'  [OK] Clip {clip.id}: {old_filename} -> {new_filename}', fg='green'))
            migrated += 1
        except Exception as e:
            click.echo(click.style(f'  [ERROR] Clip {clip.id}: {e}', fg='red'))
            errors += 1
    
    # Migrate thumbnails
    click.echo('')
    click.echo('Migrating thumbnails...')
    click.echo('')
    
    for clip in clips:
        if not clip.thumbnail_path:
            continue
        
        if re.match(r'clip_\d{3}_thumb\.jpg$', os.path.basename(clip.thumbnail_path)):
            continue
        
        old_thumb_path = os.path.join(upload_folder, clip.thumbnail_path)
        new_thumb_filename = f"clip_{clip.id:03d}_thumb.jpg"
        new_thumb_path = os.path.join(clips_folder, str(clip.use_case_id), 'thumbnails', new_thumb_filename)
        new_thumb_db_path = f"clips/{clip.use_case_id}/thumbnails/{new_thumb_filename}"
        
        if os.path.exists(old_thumb_path):
            try:
                os.makedirs(os.path.dirname(new_thumb_path), exist_ok=True)
                os.rename(old_thumb_path, new_thumb_path)
                clip.thumbnail_path = new_thumb_db_path
                click.echo(click.style(f'  [OK] Clip {clip.id} thumbnail migrated', fg='green'))
            except Exception as e:
                click.echo(click.style(f'  [ERROR] Clip {clip.id} thumbnail: {e}', fg='red'))
    
    try:
        db.session.commit()
        click.echo('')
        click.echo(click.style('Migration complete!', fg='green'))
        click.echo(f'  Migrated: {migrated}')
        click.echo(f'  Skipped: {skipped}')
        click.echo(f'  Errors: {errors}')
    except Exception as e:
        db.session.rollback()
        click.echo(click.style(f'ERROR: Failed to commit changes: {e}', fg='red'))
        raise click.Abort()


def init_cli(app):
    """Register CLI commands with the Flask app."""
    app.cli.add_command(create_admin_command)
    app.cli.add_command(list_users_command)
    app.cli.add_command(reset_password_command)
    app.cli.add_command(migrate_clip_filenames_command)
