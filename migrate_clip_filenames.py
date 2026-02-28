"""Migration script to rename existing clip files to new naming format.

Old format: clip_{id}.mp4 (e.g., clip_13.mp4)
New format: clip_{id:03d}_{sequence_order:02d}.mp4 (e.g., clip_013_01.mp4)

Run this script from the project root:
    python migrate_clip_filenames.py

Or as a Flask CLI command:
    flask migrate-clip-filenames
"""
import os
import re
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask
from app import create_app, db
from app.models import VideoClip


def migrate_clip_filenames():
    """Rename all clip files from old format to new format."""
    app = create_app()
    
    with app.app_context():
        print("=" * 60)
        print("Clip Filename Migration Tool")
        print("=" * 60)
        print()
        
        # Get upload folder
        upload_folder = app.config.get('UPLOAD_FOLDER', './uploads')
        clips_folder = os.path.join(upload_folder, 'clips')
        
        if not os.path.exists(clips_folder):
            print(f"Clips folder not found: {clips_folder}")
            return
        
        print(f"Upload folder: {upload_folder}")
        print(f"Clips folder: {clips_folder}")
        print()
        
        # Get all clips from database
        clips = VideoClip.query.all()
        print(f"Found {len(clips)} clips in database")
        print()
        
        migrated = 0
        skipped = 0
        errors = 0
        
        for clip in clips:
            if not clip.file_path:
                print(f"  [SKIP] Clip {clip.id}: No file_path")
                skipped += 1
                continue
            
            # Check if already in new format
            if re.match(r'clip_\d{3}_\d{2}\.mp4$', os.path.basename(clip.file_path)):
                print(f"  [SKIP] Clip {clip.id}: Already in new format ({clip.file_path})")
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
                # Try to find file with current file_path
                current_full_path = os.path.join(upload_folder, clip.file_path)
                if os.path.exists(current_full_path):
                    old_path = current_full_path
                else:
                    print(f"  [ERROR] Clip {clip.id}: File not found - {old_path}")
                    errors += 1
                    continue
            
            try:
                # Rename the file
                os.rename(old_path, new_path)
                
                # Update database record
                clip.file_path = new_db_path
                
                print(f"  [OK] Clip {clip.id}: {old_filename} -> {new_filename}")
                migrated += 1
                
            except Exception as e:
                print(f"  [ERROR] Clip {clip.id}: {e}")
                errors += 1
        
        # Also migrate thumbnails
        print()
        print("Migrating thumbnails...")
        print()
        
        for clip in clips:
            if not clip.thumbnail_path:
                continue
            
            # Check if already in new format
            if re.match(r'clip_\d{3}_thumb\.jpg$', os.path.basename(clip.thumbnail_path)):
                continue
            
            # Old thumbnail format: clip_{id}_thumb.jpg or similar
            old_thumb_path = os.path.join(upload_folder, clip.thumbnail_path)
            
            # New format: clip_{id:03d}_thumb.jpg
            new_thumb_filename = f"clip_{clip.id:03d}_thumb.jpg"
            new_thumb_path = os.path.join(clips_folder, str(clip.use_case_id), 'thumbnails', new_thumb_filename)
            new_thumb_db_path = f"clips/{clip.use_case_id}/thumbnails/{new_thumb_filename}"
            
            if os.path.exists(old_thumb_path):
                try:
                    # Ensure thumbnails directory exists
                    os.makedirs(os.path.dirname(new_thumb_path), exist_ok=True)
                    
                    # Rename thumbnail
                    os.rename(old_thumb_path, new_thumb_path)
                    
                    # Update database record
                    clip.thumbnail_path = new_thumb_db_path
                    
                    print(f"  [OK] Clip {clip.id} thumbnail: {os.path.basename(old_thumb_path)} -> {new_thumb_filename}")
                except Exception as e:
                    print(f"  [ERROR] Clip {clip.id} thumbnail: {e}")
        
        # Commit all database changes
        try:
            db.session.commit()
            print()
            print("=" * 60)
            print(f"Migration complete!")
            print(f"  Migrated: {migrated}")
            print(f"  Skipped: {skipped}")
            print(f"  Errors: {errors}")
            print("=" * 60)
        except Exception as e:
            db.session.rollback()
            print()
            print(f"ERROR: Failed to commit database changes: {e}")
            print("Changes rolled back.")


if __name__ == '__main__':
    migrate_clip_filenames()
