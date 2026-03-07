import re
import secrets
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app import db


ROLE_ORDER = {
    'viewer': 0,
    'member': 1,
    'admin': 2,
    'owner': 3,
}


class Brand(db.Model):
    """Brand/tenant configuration and per-brand API keys."""

    __tablename__ = 'brands'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(150), unique=True, nullable=False)
    pollo_api_key = db.Column(db.String(500))
    elevenlabs_api_key = db.Column(db.String(500))
    openai_api_key = db.Column(db.String(500))
    settings = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    memberships = db.relationship('BrandMembership', back_populates='brand', cascade='all, delete-orphan')
    invitations = db.relationship('BrandInvitation', back_populates='brand', cascade='all, delete-orphan')
    usage_records = db.relationship('UsageRecord', back_populates='brand', cascade='all, delete-orphan')
    products = db.relationship('Product', back_populates='brand_rel')
    use_cases = db.relationship('UseCase', back_populates='brand')
    video_clips = db.relationship('VideoClip', back_populates='brand')
    final_videos = db.relationship('FinalVideo', back_populates='brand')
    clip_library_entries = db.relationship('ClipLibrary', back_populates='brand')
    activity_logs = db.relationship('ActivityLog', back_populates='brand')

    @staticmethod
    def slugify(name: str) -> str:
        if not name:
            return 'brand'
        slug = name.strip().lower()
        slug = re.sub(r'[^a-z0-9]+', '-', slug)
        slug = re.sub(r'-{2,}', '-', slug).strip('-')
        return slug or 'brand'

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'slug': self.slug,
            'pollo_api_key': bool(self.pollo_api_key),
            'elevenlabs_api_key': bool(self.elevenlabs_api_key),
            'openai_api_key': bool(self.openai_api_key),
            'settings': self.settings or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class BrandMembership(db.Model):
    """Link a user to a brand with a specific role."""

    __tablename__ = 'brand_memberships'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=False)
    role = db.Column(db.String(50), default='member')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', back_populates='brand_memberships')
    brand = db.relationship('Brand', back_populates='memberships')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'brand_id', name='uq_user_brand'),
    )

    def has_min_role(self, role: str) -> bool:
        target = ROLE_ORDER.get(role, 0)
        current = ROLE_ORDER.get(self.role or 'viewer', 0)
        return current >= target

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'brand_id': self.brand_id,
            'role': self.role,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'user': self.user.username if self.user else None,
        }


class BrandInvitation(db.Model):
    """Invitation for a user to join a brand."""

    __tablename__ = 'brand_invitations'

    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default='member')
    token = db.Column(db.String(128), unique=True, nullable=False)
    invited_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)
    accepted_at = db.Column(db.DateTime)

    brand = db.relationship('Brand', back_populates='invitations')
    invited_by = db.relationship('User', back_populates='sent_invitations')

    EXPIRY_DAYS = 14

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and datetime.utcnow() > self.expires_at)

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)

    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'email': self.email,
            'role': self.role,
            'token': self.token,
            'invited_by_id': self.invited_by_id,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'accepted_at': self.accepted_at.isoformat() if self.accepted_at else None,
            'is_expired': self.is_expired,
        }


class UsageRecord(db.Model):
    """Track external API usage per brand."""

    __tablename__ = 'usage_records'

    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    service = db.Column(db.String(50), nullable=False)
    operation = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    units_consumed = db.Column(db.Float)
    estimated_cost_usd = db.Column(db.Float)
    meta_data = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    brand = db.relationship('Brand', back_populates='usage_records')
    user = db.relationship('User', back_populates='usage_records')

    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'user_id': self.user_id,
            'service': self.service,
            'operation': self.operation,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'units_consumed': self.units_consumed,
            'estimated_cost_usd': self.estimated_cost_usd,
            'meta_data': self.meta_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class User(UserMixin, db.Model):
    """User model for authentication."""
    
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    active_brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    
    brand_memberships = db.relationship('BrandMembership', back_populates='user', cascade='all, delete-orphan')
    active_brand = db.relationship('Brand', foreign_keys=[active_brand_id])
    usage_records = db.relationship('UsageRecord', back_populates='user')
    sent_invitations = db.relationship('BrandInvitation', back_populates='invited_by')
    
    def set_password(self, password):
        """Hash and set the user password."""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Check if provided password matches the hash."""
        return check_password_hash(self.password_hash, password)
    
    def get_membership(self, brand_id):
        return next((m for m in self.brand_memberships if m.brand_id == brand_id), None)

    def has_brand_access(self, brand_id, min_role='viewer') -> bool:
        membership = self.get_membership(brand_id)
        if not membership:
            return False
        return membership.has_min_role(min_role)
    
    def __repr__(self):
        return f'<User {self.username}>'


class Product(db.Model):
    """Product model for storing scraped product data."""
    
    __tablename__ = 'products'
    
    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'))
    name = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text)
    brand = db.Column(db.String(100))
    price = db.Column(db.String(50))
    currency = db.Column(db.String(10))
    images = db.Column(db.JSON, default=list)  # List of image URLs/paths
    specifications = db.Column(db.JSON, default=dict)  # Product specs
    reviews = db.Column(db.JSON, default=list)  # Customer reviews
    scraped_data = db.Column(db.JSON, default=dict)  # Raw scraped data
    status = db.Column(db.String(50), default='pending')  # pending, scraped, error
    pipeline_state = db.Column(db.JSON, default=dict)  # Tracks pipeline progress for recovery
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    brand_rel = db.relationship('Brand', back_populates='products')
    use_cases = db.relationship('UseCase', backref='product', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Product {self.name}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'name': self.name,
            'url': self.url,
            'description': self.description,
            'brand': self.brand,
            'price': self.price,
            'currency': self.currency,
            'images': self.images,
            'specifications': self.specifications,
            'reviews': self.reviews,
            'status': self.status,
            'pipeline_state': getattr(self, 'pipeline_state', None) or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    def get_current_stage_info(self):
        """Return an overview of the product's pipeline status."""

        use_cases = UseCase.query.filter_by(product_id=self.id).order_by(UseCase.created_at.desc()).all()
        use_case = use_cases[0] if use_cases else None
        use_case_id = use_case.id if use_case else None
        use_case_name = use_case.name if use_case else None

        script = None
        clips = []
        complete_clips = []
        error_clips = []
        pending_clips = []
        target_clips = 0
        final_video = None
        hook = Hook.query.filter_by(use_case_id=use_case_id).first() if use_case_id else None

        if use_case_id:
            script = Script.query.filter_by(use_case_id=use_case_id).order_by(Script.created_at.desc()).first()
            clips = VideoClip.query.filter_by(use_case_id=use_case_id).all()
            complete_clips = [c for c in clips if c.status == 'complete']
            error_clips = [c for c in clips if c.status == 'error']
            pending_clips = [c for c in clips if c.status in ('pending', 'generating')]
            target_clips = use_case.num_clips or use_case.calculated_num_clips or 4
            final_video = FinalVideo.query.filter_by(use_case_id=use_case_id).order_by(FinalVideo.created_at.desc()).first()

        clip_stats = {
            'complete': len(complete_clips),
            'errors': len(error_clips),
            'pending': len(pending_clips),
            'target': target_clips
        }

        stage_order = ['scrape', 'spec', 'hook', 'script', 'video', 'assembly', 'output']
        use_case_url = f"/use-case/{self.id}"
        hook_url = f"/hook/{use_case_id}" if use_case_id else use_case_url
        script_url = f"/script/{use_case_id}" if use_case_id else use_case_url
        video_url = f"/video-gen/{use_case_id}" if use_case_id else use_case_url
        assembly_url = f"/assembly/{use_case_id}" if use_case_id else use_case_url
        output_url = f"/output/{use_case_id}" if use_case_id else use_case_url

        stage_templates = {
            'scrape': {'label': 'Scrape', 'description': 'Import product data', 'url': '/scrape'},
            'spec': {'label': 'Spec', 'description': 'Configure use case & voice', 'url': use_case_url},
            'hook': {'label': 'Hook', 'description': 'Choose and preview your hook', 'url': hook_url},
            'script': {'label': 'Script', 'description': 'Generate and approve script', 'url': script_url},
            'video': {'label': 'Video', 'description': 'Generate required clips', 'url': video_url},
            'assembly': {'label': 'Assembly', 'description': 'Assemble clips with voiceover', 'url': assembly_url},
            'output': {'label': 'Output', 'description': 'Download & share final video', 'url': output_url}
        }

        stage_progress = {
            'scrape': 12.5,
            'spec': 25.0,
            'hook': 40.0,
            'script': 55.0,
            'video': 75.0,
            'assembly': 90.0,
            'output': 100.0
        }

        stage_status_map = {
            key: {
                'status': 'pending',
                'summary': tmpl['description'],
                'enabled': key == 'scrape'
            }
            for key, tmpl in stage_templates.items()
        }

        def set_stage_state(key, status, summary, enabled=None):
            entry = stage_status_map.get(key, {}).copy()
            entry['status'] = status
            entry['summary'] = summary
            if enabled is None:
                entry['enabled'] = status in ('current', 'complete')
            else:
                entry['enabled'] = bool(enabled)
            stage_status_map[key] = entry

        def build_response(current_stage_key: str, stage_label: str, progress_value: float, is_complete: bool = False):
            steps = []
            for key in stage_order:
                template = stage_templates[key]
                status_meta = stage_status_map.get(key, {})
                steps.append({
                    'key': key,
                    'label': template['label'],
                    'description': template['description'],
                    'summary': status_meta.get('summary', template['description']),
                    'status': status_meta.get('status', 'pending'),
                    'url': template['url'],
                    'enabled': bool(status_meta.get('enabled', False) and template['url']),
                    'progress_pct': stage_progress[key],
                    'is_current': status_meta.get('status') == 'current',
                    'is_complete': status_meta.get('status') == 'complete'
                })
            next_url = stage_templates.get(current_stage_key, {}).get('url') or '/scrape'
            pending_step = next((step for step in steps if step['status'] == 'current'), None)
            if pending_step and pending_step.get('url'):
                next_url = pending_step['url']
            return {
                'current_stage': current_stage_key,
                'stage_label': stage_label,
                'progress_pct': round(progress_value, 1),
                'next_url': next_url,
                'use_case_id': use_case_id,
                'use_case_name': use_case_name,
                'is_complete': is_complete,
                'pipeline_steps': steps,
                'clip_stats': clip_stats,
                'final_video_status': final_video.status if final_video else None
            }

        progress_pct = 0.0

        product_status = (self.status or '').lower()
        scrape_complete = bool(self.scraped_data) or product_status not in ('', 'pending', 'error')
        if scrape_complete:
            set_stage_state('scrape', 'complete', 'Product scraped', enabled=True)
            progress_pct = stage_progress['scrape']
        else:
            set_stage_state('scrape', 'current', 'Scrape a product URL to begin', enabled=True)
            return build_response('scrape', 'Scrape a product to get started', 0.0)

        if not use_case:
            set_stage_state('spec', 'current', 'Set up your first use case', enabled=True)
            return build_response('spec', 'Configure your use case details', progress_pct)
        else:
            set_stage_state('spec', 'complete', f'Use case: {use_case.name}', enabled=True)
            progress_pct = max(progress_pct, stage_progress['spec'])

        if not hook:
            set_stage_state('hook', 'current', 'Generate hook concepts', enabled=True)
            return build_response('hook', 'Choose your hook style', progress_pct)

        hook_status = (hook.status or '').lower()
        if hook_status == 'failed':
            summary = hook.error_message or 'Hook previews failed'
            set_stage_state('hook', 'current', summary, enabled=True)
            return build_response('hook', summary, progress_pct)

        if not (hook.image_paths or []):
            set_stage_state('hook', 'current', 'Generate preview assets', enabled=True)
            return build_response('hook', 'Generate hook previews', progress_pct)

        if hook.winning_variant_index is None:
            set_stage_state('hook', 'current', 'Select your winning variant', enabled=True)
            return build_response('hook', 'Select your favorite hook', stage_progress['hook'])

        hook_summary = 'Hook animated' if hook_status == 'complete' else 'Hook selected'
        if hook_status == 'animating':
            hook_summary = 'Hook animation in progress'
        set_stage_state('hook', 'complete', hook_summary, enabled=True)
        progress_pct = max(progress_pct, stage_progress['hook'])

        if not script:
            set_stage_state('script', 'current', 'Generate your script', enabled=True)
            return build_response('script', 'Generate a script for this use case', progress_pct)

        script_status = (script.status or '').lower()
        if script_status != 'approved':
            set_stage_state('script', 'current', 'Review and approve the script', enabled=True)
            mid_progress = stage_progress['hook'] + 5.0
            return build_response('script', 'Script pending approval', mid_progress)

        set_stage_state('script', 'complete', 'Script approved', enabled=True)
        progress_pct = max(progress_pct, stage_progress['script'])

        clip_ratio = 1.0 if target_clips <= 0 else len(complete_clips) / max(target_clips, 1)

        if clip_ratio < 1.0:
            if error_clips:
                label = f"Video - {len(error_clips)} error(s)"
            elif pending_clips:
                label = f"Video - {len(complete_clips)}/{target_clips} clips ready"
            else:
                label = 'Video - Starting generation'

            set_stage_state('video', 'current', label, enabled=True)

            clip_progress = clip_ratio * (stage_progress['video'] - stage_progress['script'])
            return build_response('video', label, stage_progress['script'] + clip_progress)

        set_stage_state('video', 'complete', 'All required clips complete', enabled=True)
        progress_pct = max(progress_pct, stage_progress['video'])

        if not final_video:
            set_stage_state('assembly', 'current', 'Ready for assembly', enabled=True)
            return build_response('assembly', 'Ready for assembly', progress_pct)

        final_status = (final_video.status or '').lower()
        if final_status == 'error':
            set_stage_state('assembly', 'current', 'Assembly failed - retry', enabled=True)
            return build_response('assembly', 'Assembly failed - retry needed', progress_pct)
        elif final_status in ('pending', 'assembling'):
            set_stage_state('assembly', 'current', 'Assembling video...', enabled=True)
            return build_response('assembly', 'Assembling video...', stage_progress['assembly'] - 5.0)

        set_stage_state('assembly', 'complete', 'Assembly finished', enabled=True)
        progress_pct = max(progress_pct, stage_progress['assembly'])

        if final_status == 'complete':
            set_stage_state('output', 'complete', 'Final video ready', enabled=True)
            return build_response('output', 'Final video ready to download', stage_progress['output'], is_complete=True)

        set_stage_state('output', 'current', 'Finalize output', enabled=True)
        return build_response('output', 'Prepare final output', progress_pct)


class UseCase(db.Model):
    """Use case configuration for video generation."""
    
    __tablename__ = 'use_cases'
    
    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    
    format = db.Column(db.String(20), default='9:16')
    style = db.Column(db.String(50), default='realistic')
    goal = db.Column(db.String(200))
    target_audience = db.Column(db.String(200))
    duration_target = db.Column(db.Integer, default=15)
    
    voice_id = db.Column(db.String(100))
    voice_settings = db.Column(db.JSON, default=dict)
    
    num_clips = db.Column(db.Integer, default=4)
    generation_mode = db.Column(db.String(50), default='balanced')
    clip_strategy_overrides = db.Column(db.JSON, default=dict)
    status = db.Column(db.String(50), default='draft')
    pipeline_state = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    brand = db.relationship('Brand', back_populates='use_cases')
    scripts = db.relationship('Script', backref='use_case', lazy=True, cascade='all, delete-orphan')
    video_clips = db.relationship('VideoClip', backref='use_case', lazy=True, cascade='all, delete-orphan')
    final_videos = db.relationship('FinalVideo', backref='use_case', lazy=True, cascade='all, delete-orphan')
    hook = db.relationship('Hook', back_populates='use_case', uselist=False, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<UseCase {self.name}>'
    
    @staticmethod
    def calculate_num_clips(duration_seconds: int) -> int:
        if not duration_seconds:
            return 4

        duration_seconds = int(duration_seconds)
        if duration_seconds < 10:
            duration_seconds = 10

        if duration_seconds <= 15:
            return 3
        if duration_seconds <= 25:
            return 4
        if duration_seconds <= 40:
            return 5
        return 6

    @property
    def calculated_num_clips(self) -> int:
        return self.calculate_num_clips(self.duration_target or 0)

    def sync_num_clips(self):
        self.num_clips = self.calculated_num_clips

    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'product_id': self.product_id,
            'name': self.name,
            'format': self.format,
            'style': self.style,
            'goal': self.goal,
            'target_audience': self.target_audience,
            'duration_target': self.duration_target,
            'voice_id': self.voice_id,
            'voice_settings': self.voice_settings,
            'num_clips': self.num_clips,
            'generation_mode': self.generation_mode,
            'clip_strategy_overrides': self.clip_strategy_overrides or {},
            'calculated_num_clips': self.calculated_num_clips,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class Hook(db.Model):
    """Stores hook configurations and generated assets for a use case."""

    __tablename__ = 'hooks'

    id = db.Column(db.Integer, primary_key=True)
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False, unique=True)

    hook_type = db.Column(db.String(50), nullable=False)
    winning_variant_index = db.Column(db.Integer, nullable=True)

    variants = db.Column(db.JSON, default=list)
    image_paths = db.Column(db.JSON, default=list)
    audio_path = db.Column(db.String(500))
    video_path = db.Column(db.String(500))

    status = db.Column(db.String(20), default='draft')
    error_message = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    use_case = db.relationship('UseCase', back_populates='hook')

    def __repr__(self):
        return f'<Hook use_case={self.use_case_id} type={self.hook_type}>'

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'use_case_id': self.use_case_id,
            'hook_type': self.hook_type,
            'winning_variant_index': self.winning_variant_index,
            'variants': self.variants or [],
            'image_paths': self.image_paths or [],
            'audio_path': self.audio_path,
            'video_path': self.video_path,
            'status': self.status,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class Script(db.Model):
    """Voiceover script for video."""
    
    __tablename__ = 'scripts'
    
    id = db.Column(db.Integer, primary_key=True)
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False)
    
    content = db.Column(db.Text, nullable=False)
    estimated_duration = db.Column(db.Integer)
    tone = db.Column(db.String(50))
    
    status = db.Column(db.String(50), default='draft')
    generation_prompt = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<Script {self.id}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'use_case_id': self.use_case_id,
            'content': self.content,
            'estimated_duration': self.estimated_duration,
            'tone': self.tone,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class VideoClip(db.Model):
    """Individual video clip generated by Pollo.ai."""
    
    __tablename__ = 'video_clips'
    
    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'))
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False)
    
    sequence_order = db.Column(db.Integer, default=0)
    prompt = db.Column(db.Text)
    generation_strategy = db.Column(db.String(50), default='composite_then_kling')
    asset_source = db.Column(db.String(50), default='product_image')
    script_segment_ref = db.Column(db.Text)
    quality_score = db.Column(db.Float)
    
    file_path = db.Column(db.String(500))
    thumbnail_path = db.Column(db.String(500))
    
    pollo_job_id = db.Column(db.String(100))
    pollo_video_url = db.Column(db.String(1000))
    model_used = db.Column(db.String(50))
    
    duration = db.Column(db.Float)
    content_description = db.Column(db.Text)
    tags = db.Column(db.JSON, default=list)
    analysis_metadata = db.Column(db.JSON, default=dict)
    
    status = db.Column(db.String(50), default='pending')
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    brand = db.relationship('Brand', back_populates='video_clips')

    READY_STATUSES = ('complete', 'ready')
    
    def __repr__(self):
        return f'<VideoClip {self.id}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'use_case_id': self.use_case_id,
            'sequence_order': self.sequence_order,
            'prompt': self.prompt,
            'generation_strategy': self.generation_strategy,
            'asset_source': self.asset_source,
            'script_segment_ref': self.script_segment_ref,
            'quality_score': self.quality_score,
            'file_path': self.file_path,
            'thumbnail_path': self.thumbnail_path,
            'pollo_job_id': self.pollo_job_id,
            'pollo_video_url': self.pollo_video_url,
            'model_used': self.model_used,
            'duration': self.duration,
            'content_description': self.content_description,
            'tags': self.tags,
            'analysis_metadata': self.analysis_metadata,
            'content_type': self.infer_content_type(),
            'status': self.status,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None
        }

    def is_ready(self) -> bool:
        return (self.status or '').lower() in self.READY_STATUSES

    def infer_content_type(self):
        metadata = self.analysis_metadata or {}
        if metadata.get('recommended_role'):
            return metadata['recommended_role']
        if metadata.get('primary_category'):
            return metadata['primary_category']

        if self.tags:
            lowered = [str(tag).lower() for tag in self.tags]
            mapping = {
                'hook': ['hook', 'attention', 'scroll', 'intro'],
                'problem': ['problem', 'pain', 'before', 'issue'],
                'solution': ['solution', 'after', 'benefit', 'result'],
                'product': ['product', 'hero', 'showcase'],
                'demo': ['demo', 'tutorial', 'hands-on'],
                'lifestyle': ['lifestyle', 'scene', 'context'],
                'social_proof': ['social', 'testimonial', 'review', 'crowd'],
                'cta': ['cta', 'call-to-action', 'closing', 'outro'],
                'emotion': ['emotion', 'reaction', 'feel'],
                'motion': ['motion', 'movement', 'dynamic']
            }
            for category, keywords in mapping.items():
                if any(keyword in tag for tag in lowered for keyword in keywords):
                    return category

        prompt = (self.prompt or '').lower()
        prompt_keywords = {
            'hook': ['hook', 'attention', 'scroll'],
            'problem': ['problem', 'pain'],
            'solution': ['solution', 'benefit'],
            'product': ['product', 'showcase'],
            'demo': ['demo', 'tutorial'],
            'cta': ['cta', 'closing', 'call-to-action'],
            'lifestyle': ['lifestyle', 'scene'],
            'social_proof': ['testimonial', 'social', 'proof']
        }
        for category, keywords in prompt_keywords.items():
            if any(keyword in prompt for keyword in keywords):
                return category

        return 'general'


class ClipLibrary(db.Model):
    """Shared clip library for reusable video clips across products and use cases."""
    
    __tablename__ = 'clip_library'
    
    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'))
    
    original_clip_id = db.Column(db.Integer, db.ForeignKey('video_clips.id'), nullable=True)
    original_product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    original_use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=True)
    
    file_path = db.Column(db.String(500), nullable=False)
    thumbnail_path = db.Column(db.String(500))
    
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    content_type = db.Column(db.String(50))
    style = db.Column(db.String(50))
    format = db.Column(db.String(20))
    duration = db.Column(db.Float)
    
    tags = db.Column(db.JSON, default=list)
    
    prompt = db.Column(db.Text)
    model_used = db.Column(db.String(50))
    
    rating = db.Column(db.Integer, default=0)
    is_favorite = db.Column(db.Boolean, default=False)
    usage_count = db.Column(db.Integer, default=0)
    
    status = db.Column(db.String(50), default='active')
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    added_to_library_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    brand = db.relationship('Brand', back_populates='clip_library_entries')
    original_clip = db.relationship('VideoClip', backref='library_entries')
    original_product = db.relationship('Product', backref='library_clips')
    
    def __repr__(self):
        return f'<ClipLibrary {self.name}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'name': self.name,
            'description': self.description,
            'content_type': self.content_type,
            'style': self.style,
            'format': self.format,
            'duration': self.duration,
            'tags': self.tags,
            'prompt': self.prompt,
            'model_used': self.model_used,
            'rating': self.rating,
            'is_favorite': self.is_favorite,
            'usage_count': self.usage_count,
            'file_path': self.file_path,
            'thumbnail_path': self.thumbnail_path,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'added_to_library_at': self.added_to_library_at.isoformat() if self.added_to_library_at else None
        }


class UseCaseLibraryClip(db.Model):
    """Association table linking library clips to use cases."""
    
    __tablename__ = 'use_case_library_clips'
    
    id = db.Column(db.Integer, primary_key=True)
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False)
    library_clip_id = db.Column(db.Integer, db.ForeignKey('clip_library.id'), nullable=False)
    sequence_order = db.Column(db.Integer, default=0)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    library_clip = db.relationship('ClipLibrary', backref='use_case_links')
    
    def __repr__(self):
        return f'<UseCaseLibraryClip {self.use_case_id}:{self.library_clip_id}>'


class FinalVideo(db.Model):
    """Final assembled video with voiceover."""
    
    __tablename__ = 'final_videos'
    
    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'))
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False)
    script_id = db.Column(db.Integer, db.ForeignKey('scripts.id'))
    
    file_path = db.Column(db.String(500))
    thumbnail_path = db.Column(db.String(500))
    voiceover_path = db.Column(db.String(500))
    
    duration = db.Column(db.Float)
    resolution = db.Column(db.String(20))
    file_size = db.Column(db.Integer)
    
    clip_ids = db.Column(db.JSON, default=list)
    assembly_settings = db.Column(db.JSON, default=dict)
    
    status = db.Column(db.String(50), default='pending')
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    brand = db.relationship('Brand', back_populates='final_videos')
    script = db.relationship('Script', backref='final_videos')
    
    def __repr__(self):
        return f'<FinalVideo {self.id}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'use_case_id': self.use_case_id,
            'script_id': self.script_id,
            'file_path': self.file_path,
            'thumbnail_path': self.thumbnail_path,
            'voiceover_path': self.voiceover_path,
            'duration': self.duration,
            'resolution': self.resolution,
            'file_size': self.file_size,
            'clip_ids': self.clip_ids,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None
        }


class ActivityLog(db.Model):
    """Timeline of significant pipeline events for dashboard + auditing."""

    __tablename__ = 'activity_logs'

    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    event_type = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(50), default='info')
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    meta_data = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    brand = db.relationship('Brand', back_populates='activity_logs')
    user = db.relationship('User')

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'user_id': self.user_id,
            'event_type': self.event_type,
            'title': self.title,
            'description': self.description,
            'status': self.status,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'meta_data': self.meta_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
