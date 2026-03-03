from datetime import datetime
from app import db
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


class User(UserMixin, db.Model):
    """User model for authentication."""
    
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    
    def set_password(self, password):
        """Hash and set the user password."""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Check if provided password matches the hash."""
        return check_password_hash(self.password_hash, password)
    
    def __repr__(self):
        return f'<User {self.username}>'


class Product(db.Model):
    """Product model for storing scraped product data."""
    
    __tablename__ = 'products'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(500), nullable=False, unique=True)
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
    use_cases = db.relationship('UseCase', backref='product', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Product {self.name}>'
    
    def to_dict(self):
        return {
            'id': self.id,
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
        """Get the current pipeline stage information for this product.
        
        Returns a dict with:
        - current_stage: the current stage name (scrape, usecase, script, video_gen, assembly, output)
        - stage_label: human-readable label
        - progress_pct: approximate completion percentage
        - next_url: URL to continue work
        - use_case_id: current use case ID (if any)
        - use_case_name: current use case name (if any)
        - is_complete: whether the project is fully complete
        """
        from app import db
        
        # Check for use cases
        use_cases = UseCase.query.filter_by(product_id=self.id).order_by(UseCase.created_at.desc()).all()
        
        if not use_cases:
            return {
                'current_stage': 'usecase',
                'stage_label': 'Scraped - Needs Use Case',
                'progress_pct': 14.3,
                'next_url': f"/use-case/{self.id}",
                'use_case_id': None,
                'use_case_name': None,
                'is_complete': False
            }
        
        # Get most recent use case
        use_case = use_cases[0]
        
        # Check script status
        script = Script.query.filter_by(use_case_id=use_case.id).first()
        
        if not script:
            return {
                'current_stage': 'script',
                'stage_label': 'Needs Script',
                'progress_pct': 28.6,
                'next_url': f"/script/{use_case.id}",
                'use_case_id': use_case.id,
                'use_case_name': use_case.name,
                'is_complete': False
            }
        
        if script.status != 'approved':
            return {
                'current_stage': 'script',
                'stage_label': 'Script Pending Approval',
                'progress_pct': 42.9,
                'next_url': f"/script/{use_case.id}",
                'use_case_id': use_case.id,
                'use_case_name': use_case.name,
                'is_complete': False
            }
        
        # Check video clips
        clips = VideoClip.query.filter_by(use_case_id=use_case.id).all()
        complete_clips = [c for c in clips if c.status == 'complete']
        error_clips = [c for c in clips if c.status == 'error']
        pending_clips = [c for c in clips if c.status in ('pending', 'generating')]
        
        target_clips = use_case.num_clips or use_case.calculated_num_clips or 4
        
        # If not all clips are complete, we're in video_gen stage
        if len(complete_clips) < target_clips:
            if error_clips:
                label = f'Video Gen - {len(error_clips)} Error(s)'
            elif pending_clips:
                label = f'Video Gen - {len(complete_clips)}/{target_clips} Complete'
            else:
                label = 'Video Gen - Starting'
            
            clip_progress = (len(complete_clips) / max(target_clips, 1)) * 14.3
            
            return {
                'current_stage': 'video_gen',
                'stage_label': label,
                'progress_pct': 42.9 + clip_progress,
                'next_url': f"/video-gen/{use_case.id}",
                'use_case_id': use_case.id,
                'use_case_name': use_case.name,
                'is_complete': False
            }
        
        # All clips complete - check assembly status
        final_video = FinalVideo.query.filter_by(use_case_id=use_case.id).order_by(FinalVideo.created_at.desc()).first()
        
        if not final_video:
            return {
                'current_stage': 'assembly',
                'stage_label': 'Ready for Assembly',
                'progress_pct': 71.4,
                'next_url': f"/assembly/{use_case.id}",
                'use_case_id': use_case.id,
                'use_case_name': use_case.name,
                'is_complete': False
            }
        
        if final_video.status == 'complete':
            return {
                'current_stage': 'output',
                'stage_label': 'Final Video Ready',
                'progress_pct': 100.0,
                'next_url': f"/output/{use_case.id}",
                'use_case_id': use_case.id,
                'use_case_name': use_case.name,
                'is_complete': True
            }
        elif final_video.status == 'error':
            return {
                'current_stage': 'assembly',
                'stage_label': 'Assembly Failed - Retry',
                'progress_pct': 71.4,
                'next_url': f"/assembly/{use_case.id}",
                'use_case_id': use_case.id,
                'use_case_name': use_case.name,
                'is_complete': False
            }
        else:
            return {
                'current_stage': 'assembly',
                'stage_label': 'Assembling Video...',
                'progress_pct': 85.7,
                'next_url': f"/assembly/{use_case.id}",
                'use_case_id': use_case.id,
                'use_case_name': use_case.name,
                'is_complete': False
            }

class UseCase(db.Model):
    """Use case configuration for video generation."""
    
    __tablename__ = 'use_cases'
    
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)  # e.g., "Product Demo"
    
    # Video configuration
    format = db.Column(db.String(20), default='9:16')  # 9:16, 16:9, 1:1, 4:5
    style = db.Column(db.String(50), default='realistic')  # realistic, animated, comic, cinematic
    goal = db.Column(db.String(200))  # Call to action text
    target_audience = db.Column(db.String(200))
    duration_target = db.Column(db.Integer, default=15)  # Target duration in seconds
    
    # Voice configuration
    voice_id = db.Column(db.String(100))
    voice_settings = db.Column(db.JSON, default=dict)  # Stability, similarity, etc.
    
    # Generation settings
    num_clips = db.Column(db.Integer, default=4)  # Number of video clips to generate
    status = db.Column(db.String(50), default='draft')  # draft, configured, generating, complete
    pipeline_state = db.Column(db.JSON, default=dict)  # Tracks per-stage progress for recovery
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    scripts = db.relationship('Script', backref='use_case', lazy=True, cascade='all, delete-orphan')
    video_clips = db.relationship('VideoClip', backref='use_case', lazy=True, cascade='all, delete-orphan')
    final_videos = db.relationship('FinalVideo', backref='use_case', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<UseCase {self.name}>'
    
    @staticmethod
    def calculate_num_clips(duration_seconds: int) -> int:
        """Return recommended clip count for a target duration."""
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
        """Convenience accessor for the calculated clip count."""
        return self.calculate_num_clips(self.duration_target or 0)

    def sync_num_clips(self):
        """Update the stored num_clips field based on duration_target."""
        self.num_clips = self.calculated_num_clips

    def to_dict(self):
        return {
            'id': self.id,
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
            'calculated_num_clips': self.calculated_num_clips,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class Script(db.Model):
    """Voiceover script for video."""
    
    __tablename__ = 'scripts'
    
    id = db.Column(db.Integer, primary_key=True)
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False)
    
    content = db.Column(db.Text, nullable=False)
    estimated_duration = db.Column(db.Integer)  # Estimated duration in seconds
    tone = db.Column(db.String(50))  # enthusiastic, professional, casual, etc.
    
    # Status tracking
    status = db.Column(db.String(50), default='draft')  # draft, generated, approved
    generation_prompt = db.Column(db.Text)  # The prompt used to generate this script
    
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
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False)
    
    # Clip metadata
    sequence_order = db.Column(db.Integer, default=0)  # Position in final video
    prompt = db.Column(db.Text)  # The prompt used to generate this clip
    
    # File paths
    file_path = db.Column(db.String(500))
    thumbnail_path = db.Column(db.String(500))
    
    # Pollo.ai specific
    pollo_job_id = db.Column(db.String(100))
    pollo_video_url = db.Column(db.String(1000))
    model_used = db.Column(db.String(50))  # e.g., "minimax-video-01"
    
    # Clip analysis
    duration = db.Column(db.Float)  # Duration in seconds
    content_description = db.Column(db.Text)  # AI-generated description
    tags = db.Column(db.JSON, default=list)  # Tags for categorization
    analysis_metadata = db.Column(db.JSON, default=dict)  # Additional analysis data
    
    # Status
    status = db.Column(db.String(50), default='pending')  # pending, generating, complete, error
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    READY_STATUSES = ('complete', 'ready')
    
    def __repr__(self):
        return f'<VideoClip {self.id}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'use_case_id': self.use_case_id,
            'sequence_order': self.sequence_order,
            'prompt': self.prompt,
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
        """Return True if the clip has finished generating remotely."""
        return (self.status or '').lower() in self.READY_STATUSES

    def infer_content_type(self):
        """Best-effort categorization based on analysis metadata, tags, or prompt."""
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
    
    # Source information (where this clip came from)
    original_clip_id = db.Column(db.Integer, db.ForeignKey('video_clips.id'), nullable=True)
    original_product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    original_use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=True)
    
    # File paths (copied from original clip)
    file_path = db.Column(db.String(500), nullable=False)
    thumbnail_path = db.Column(db.String(500))
    
    # Metadata for search/filter
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    content_type = db.Column(db.String(50))  # hook, problem, solution, cta, product, etc.
    style = db.Column(db.String(50))  # realistic, cinematic, animated, comic
    format = db.Column(db.String(20))  # 9:16, 16:9, 1:1, 4:5
    duration = db.Column(db.Float)
    
    # Tags for categorization
    tags = db.Column(db.JSON, default=list)
    
    # Prompt and model info (for reference)
    prompt = db.Column(db.Text)
    model_used = db.Column(db.String(50))
    
    # Quality rating (user can rate clips 1-5)
    rating = db.Column(db.Integer, default=0)  # 0 = not rated, 1-5 = star rating
    is_favorite = db.Column(db.Boolean, default=False)
    usage_count = db.Column(db.Integer, default=0)  # How many times used
    
    # Status
    status = db.Column(db.String(50), default='active')  # active, archived
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    added_to_library_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    original_clip = db.relationship('VideoClip', backref='library_entries')
    original_product = db.relationship('Product', backref='library_clips')
    
    def __repr__(self):
        return f'<ClipLibrary {self.name}>'
    
    def to_dict(self):
        return {
            'id': self.id,
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
    
    # Relationship
    library_clip = db.relationship('ClipLibrary', backref='use_case_links')
    
    def __repr__(self):
        return f'<UseCaseLibraryClip {self.use_case_id}:{self.library_clip_id}>'


class FinalVideo(db.Model):
    """Final assembled video with voiceover."""
    
    __tablename__ = 'final_videos'
    
    id = db.Column(db.Integer, primary_key=True)
    use_case_id = db.Column(db.Integer, db.ForeignKey('use_cases.id'), nullable=False)
    script_id = db.Column(db.Integer, db.ForeignKey('scripts.id'))
    
    # Video file
    file_path = db.Column(db.String(500))
    thumbnail_path = db.Column(db.String(500))
    
    # Audio
    voiceover_path = db.Column(db.String(500))
    
    # Metadata
    duration = db.Column(db.Float)  # Final duration in seconds
    resolution = db.Column(db.String(20))  # e.g., "1080x1920"
    file_size = db.Column(db.Integer)  # Size in bytes
    
    # Assembly info
    clip_ids = db.Column(db.JSON, default=list)  # Ordered list of clip IDs used
    assembly_settings = db.Column(db.JSON, default=dict)  # ffmpeg settings, transitions, etc.
    
    # Status
    status = db.Column(db.String(50), default='pending')  # pending, assembling, complete, error
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    # Relationships
    script = db.relationship('Script', backref='final_videos')
    
    def __repr__(self):
        return f'<FinalVideo {self.id}>'
    
    def to_dict(self):
        return {
            'id': self.id,
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
    event_type = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(50), default='info')
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    meta_data = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'event_type': self.event_type,
            'title': self.title,
            'description': self.description,
            'status': self.status,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'meta_data': self.meta_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

