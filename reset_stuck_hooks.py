#!/usr/bin/env python3
"""Reset stuck hooks from 'generating' to 'draft' status."""

import os
import sys

# Add the project to path
sys.path.insert(0, '/opt/render/project/src')

from app import create_app
from app.models import Hook
from app import db

app = create_app()

with app.app_context():
    # Find all hooks stuck in 'generating' status
    stuck_hooks = Hook.query.filter_by(status='generating').all()
    
    if not stuck_hooks:
        print("No hooks stuck in 'generating' status.")
        sys.exit(0)
    
    print(f"Found {len(stuck_hooks)} hook(s) stuck in 'generating' status:")
    for hook in stuck_hooks:
        print(f"  - Hook {hook.id} (use_case_id={hook.use_case_id})")
    
    # Reset them to 'draft'
    for hook in stuck_hooks:
        hook.status = 'draft'
        hook.error_message = None
    
    db.session.commit()
    print(f"\nReset {len(stuck_hooks)} hook(s) to 'draft' status.")
