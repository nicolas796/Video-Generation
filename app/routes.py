"""Routes for the Product Video Generator."""
import os
import uuid
import hmac
import hashlib
from datetime import datetime
from typing import Any, Optional, Dict

import requests
from flask import Blueprint, render_template, jsonify, request, current_app, send_from_directory, url_for, abort
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from app.models import Product, UseCase, Script, VideoClip, FinalVideo
from app import db
from app.scrapers import scrape_product
from app.services.script_gen import ScriptGenerator
from app.services.video_clip_manager import VideoClipManager
from app.services.pollo_ai import PolloAIClient
from app.services.clip_analyzer import ClipAnalyzer
from app.services.clip_ordering import ClipOrderingEngine
from app.services.voiceover import VoiceoverGenerator
from app.services.video_assembly import VideoAssembler
from app.services.pipeline_progress import PipelineProgressTracker, PipelineRecoveryService

import cv2

main_bp = Blueprint('main', __name__)


def is_safe_path(basedir, path):
    """Check if a path is safe (prevents path traversal attacks).
    
    Returns True if the resolved path is within basedir.
    """
    try:
        # Resolve to absolute paths
        base_path = os.path.abspath(basedir)
        target_path = os.path.abspath(os.path.join(basedir, path))
        
        # Check if target_path starts with base_path
        return target_path.startswith(base_path)
    except (ValueError, OSError):
        return False


def safe_join(basedir, *paths):
    """Safely join paths and check for path traversal.
    
    Returns the joined path if safe, or None if path traversal detected.
    """
    try:
        base_path = os.path.abspath(basedir)
        target_path = os.path.abspath(os.path.join(base_path, *paths))
        
        if not target_path.startswith(base_path):
            return None
        return target_path
    except (ValueError, OSError):
        return None


def _extract_nested_value(payload: Any, path: str) -> Optional[Any]:
    """Retrieve a nested value from a dict using dot notation."""
    current = payload
    for part in path.split('.'):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _extract_pollo_video_url(payload: dict) -> Optional[str]:
    """Best-effort extraction of the video URL from a Pollo webhook payload."""
    if not isinstance(payload, dict):
        return None
    
    # Check for generations array (actual Pollo webhook format)
    generations = payload.get('generations')
    if isinstance(generations, list) and len(generations) > 0:
        url = generations[0].get('url')
        if url:
            return url
    
    candidate_paths = [
        'videoUrl',
        'video_url',
        'video',
        'result.videoUrl',
        'result.video_url',
        'result.url',
        'result.output.videoUrl',
        'result.output.url',
        'data.videoUrl',
        'data.result.videoUrl',
        'output.videoUrl',
        'output.url',
        'payload.videoUrl',
        'payload.output.url',
        'assets.video',
        'local_relative_path'
    ]
    for path in candidate_paths:
        value = _extract_nested_value(payload, path) if '.' in path else payload.get(path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_pollo_status(payload: dict) -> str:
    """Normalize status field from payload."""
    # Check generations array first (actual Pollo webhook format)
    generations = payload.get('generations')
    if isinstance(generations, list) and len(generations) > 0:
        gen = generations[0]
        gen_status = gen.get('status')
        if isinstance(gen_status, str) and gen_status.strip():
            return gen_status.strip().lower()
        # If URL is empty and no status, likely failed
        if not gen.get('url'):
            return 'failed'
    
    candidates = [
        payload.get('status'),
        payload.get('taskStatus'),
        payload.get('task_status'),
        payload.get('state'),
        _extract_nested_value(payload, 'data.status'),
        _extract_nested_value(payload, 'result.status')
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return ''


def _verify_pollo_signature(secret: str, provided_signature: Optional[str], raw_body: bytes, webhook_id: Optional[str] = None, webhook_timestamp: Optional[str] = None) -> bool:
    """Validate HMAC-SHA256 signature for webhook payloads per Pollo.ai spec.
    
    The signature is calculated as HMAC-SHA256 of: webhook_id.webhook_timestamp.body
    """
    if not secret or not provided_signature:
        return False
    
    # Build the signed content: webhook_id.webhook_timestamp.body
    signed_content = ""
    if webhook_id and webhook_timestamp:
        signed_content = f"{webhook_id}.{webhook_timestamp}."
    
    if raw_body:
        signed_content = signed_content + raw_body.decode('utf-8')
    
    digest = hmac.new(secret.encode('utf-8'), signed_content.encode('utf-8'), hashlib.sha256).hexdigest()
    provided = provided_signature.split('=', 1)[-1].strip()
    return hmac.compare_digest(digest, provided)


def _download_clip_assets(clip: VideoClip, video_url: str) -> Dict[str, Optional[str]]:
    """Download a Pollo-generated clip and create a thumbnail.
    
    Returns a dictionary with relative video/thumbnail paths (relative to UPLOAD_FOLDER).
    Raises any download or file errors to the caller.
    """
    upload_root = os.path.abspath(current_app.config.get('UPLOAD_FOLDER', './uploads'))
    use_case_str = str(clip.use_case_id)
    clip_folder = os.path.join(upload_root, 'clips', use_case_str)
    os.makedirs(clip_folder, exist_ok=True)
    video_filename = f"clip_{clip.id}.mp4"
    video_path = os.path.join(clip_folder, video_filename)
    response = requests.get(video_url, stream=True, timeout=120)
    response.raise_for_status()
    with open(video_path, 'wb') as video_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                video_file.write(chunk)
    video_rel_path = os.path.relpath(video_path, upload_root)
    thumb_rel_path = _generate_clip_thumbnail(video_path, clip.use_case_id, clip.id, upload_root)
    return {
        'video': video_rel_path,
        'thumbnail': thumb_rel_path
    }


def _generate_clip_thumbnail(video_path: str, use_case_id: int, clip_id: int, upload_root: str) -> Optional[str]:
    """Generate a thumbnail image for a downloaded clip."""
    try:
        cap = cv2.VideoCapture(video_path)
    except Exception:
        return None
    if not cap.isOpened():
        cap.release()
        return None
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total_frames > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(total_frames // 2, 0))
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return None
    thumb_folder = os.path.join(upload_root, 'clips', str(use_case_id), 'thumbnails')
    os.makedirs(thumb_folder, exist_ok=True)
    thumb_filename = f"clip_{clip_id}.jpg"
    thumb_path = os.path.join(thumb_folder, thumb_filename)
    height, width = frame.shape[:2]
    max_width = 480
    if width > max_width and width > 0:
        ratio = max_width / float(width)
        frame = cv2.resize(frame, (max_width, int(height * ratio)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(thumb_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return os.path.relpath(thumb_path, upload_root)


@main_bp.route('/')
@login_required
def index():
    """Home page with pipeline visualization."""
    return render_template('index.html')


@main_bp.route('/scrape')
@login_required
def scrape_page():
    """Scraping UI page."""
    return render_template('scrape.html')


@main_bp.route('/spec-sheet/<int:product_id>')
@login_required
def spec_sheet_page(product_id):
    """Spec sheet UI page."""
    product = Product.query.get_or_404(product_id)
    return render_template('spec_sheet.html', product=product)


# ============================================================================
# API Routes
# ============================================================================

@main_bp.route('/api/status')
def api_status():
    """API status endpoint (public)."""
    return jsonify({
        'status': 'ok',
        'message': 'Product Video Generator API is running'
    })


@main_bp.route('/api/products', methods=['GET'])
@login_required
def get_products():
    """Get all products."""
    products = Product.query.order_by(Product.created_at.desc()).all()
    return jsonify([p.to_dict() for p in products])


@main_bp.route('/api/products', methods=['POST'])
@login_required
def create_product():
    """Create a new product."""
    data = request.get_json()
    product = Product(
        name=data.get('name'),
        url=data.get('url'),
        description=data.get('description')
    )
    db.session.add(product)
    db.session.commit()
    return jsonify(product.to_dict()), 201


@main_bp.route('/api/products/<int:product_id>', methods=['GET'])
@login_required
def get_product(product_id):
    """Get a single product."""
    product = Product.query.get_or_404(product_id)
    return jsonify(product.to_dict())


@main_bp.route('/api/products/<int:product_id>', methods=['PUT'])
@login_required
def update_product(product_id):
    """Update a product."""
    product = Product.query.get_or_404(product_id)
    data = request.get_json()
    
    product.name = data.get('name', product.name)
    product.description = data.get('description', product.description)
    product.brand = data.get('brand', product.brand)
    product.price = data.get('price', product.price)
    product.currency = data.get('currency', product.currency)
    product.images = data.get('images', product.images)
    product.specifications = data.get('specifications', product.specifications)
    product.reviews = data.get('reviews', product.reviews)
    
    db.session.commit()
    return jsonify(product.to_dict())


@main_bp.route('/api/products/<int:product_id>', methods=['DELETE'])
@login_required
def delete_product(product_id):
    """Delete a product."""
    product = Product.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    return jsonify({'message': 'Product deleted'})


# ============================================================================
# Scraping API Routes
# ============================================================================

@main_bp.route('/api/scrape', methods=['POST'])
@login_required
def scrape_url():
    """
    Scrape a product URL and return the data.
    Optionally saves to database if save=true.
    """
    data = request.get_json()
    url = data.get('url')
    save = data.get('save', False)
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    # Validate URL format
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL format'}), 400
    
    # Scrape the URL
    scraped_data = scrape_product(url)
    
    if 'error' in scraped_data:
        return jsonify({
            'success': False,
            'error': scraped_data['error'],
            'url': url
        }), 422
    
    # Save to database if requested
    if save:
        # Check if product already exists
        existing = Product.query.filter_by(url=scraped_data['url']).first()
        if existing:
            # Update existing product
            existing.name = scraped_data.get('name', existing.name)
            existing.description = scraped_data.get('description', existing.description)
            existing.brand = scraped_data.get('brand', existing.brand)
            existing.price = scraped_data.get('price', existing.price)
            existing.currency = scraped_data.get('currency', existing.currency)
            existing.images = scraped_data.get('images', [])
            existing.specifications = scraped_data.get('specifications', {})
            existing.reviews = scraped_data.get('reviews', [])
            existing.scraped_data = scraped_data.get('raw_data', {})
            existing.status = 'scraped'
            db.session.commit()
            product = existing
        else:
            # Create new product
            product = Product(
                name=scraped_data.get('name', 'Unknown Product'),
                url=scraped_data['url'],
                description=scraped_data.get('description', ''),
                brand=scraped_data.get('brand', ''),
                price=scraped_data.get('price', ''),
                currency=scraped_data.get('currency', ''),
                images=scraped_data.get('images', []),
                specifications=scraped_data.get('specifications', {}),
                reviews=scraped_data.get('reviews', []),
                scraped_data=scraped_data.get('raw_data', {}),
                status='scraped'
            )
            db.session.add(product)
            db.session.commit()
        
        scraped_data['product_id'] = product.id
        scraped_data['saved'] = True
        
        # Auto-download product images
        if product.images:
            try:
                product_folder = os.path.join(
                    current_app.config['PRODUCT_UPLOAD_FOLDER'],
                    str(product.id)
                )
                os.makedirs(product_folder, exist_ok=True)
                
                downloaded_count = 0
                for i, img_url in enumerate(product.images[:10]):  # Max 10 images
                    try:
                        response = requests.get(img_url, timeout=30)
                        response.raise_for_status()
                        
                        # Determine extension
                        content_type = response.headers.get('content-type', '')
                        if 'jpeg' in content_type or 'jpg' in content_type:
                            ext = 'jpg'
                        elif 'png' in content_type:
                            ext = 'png'
                        elif 'webp' in content_type:
                            ext = 'webp'
                        else:
                            ext = 'jpg'
                        
                        filename = f"image_{i+1:02d}.{ext}"
                        filepath = os.path.join(product_folder, filename)
                        
                        with open(filepath, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)
                        downloaded_count += 1
                    except Exception as e:
                        current_app.logger.warning(f"Failed to download image {img_url}: {e}")
                
                scraped_data['images_downloaded'] = downloaded_count
            except Exception as e:
                current_app.logger.error(f"Error auto-downloading images: {e}")
    else:
        scraped_data['saved'] = False
    
    return jsonify({
        'success': True,
        'data': scraped_data
    })


@main_bp.route('/api/scrape/preview', methods=['POST'])
@login_required
def scrape_preview():
    """Scrape a URL for preview (does not save to database)."""
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid URL format'}), 400
    
    scraped_data = scrape_product(url)
    
    if 'error' in scraped_data:
        return jsonify({
            'success': False,
            'error': scraped_data['error']
        }), 422
    
    return jsonify({
        'success': True,
        'data': scraped_data
    })


# ============================================================================
# Asset Management Routes
# ============================================================================

@main_bp.route('/api/products/<int:product_id>/download-images', methods=['POST'])
@login_required
def download_product_images(product_id):
    """Download product images to local storage."""
    product = Product.query.get_or_404(product_id)
    
    if not product.images:
        return jsonify({'error': 'No images to download'}), 400
    
    downloaded = []
    failed = []
    
    # Create product directory
    product_folder = os.path.join(
        current_app.config['PRODUCT_UPLOAD_FOLDER'],
        str(product_id)
    )
    os.makedirs(product_folder, exist_ok=True)
    
    for i, img_url in enumerate(product.images):
        try:
            response = requests.get(img_url, timeout=30, stream=True)
            response.raise_for_status()
            
            # Determine file extension
            content_type = response.headers.get('content-type', '')
            if 'jpeg' in content_type or 'jpg' in content_type:
                ext = 'jpg'
            elif 'png' in content_type:
                ext = 'png'
            elif 'webp' in content_type:
                ext = 'webp'
            elif 'gif' in content_type:
                ext = 'gif'
            else:
                # Try to get extension from URL
                ext = os.path.splitext(img_url.split('?')[0])[1].lstrip('.')
                if ext not in ['jpg', 'jpeg', 'png', 'webp', 'gif']:
                    ext = 'jpg'
            
            filename = f"image_{i+1:02d}.{ext}"
            filepath = os.path.join(product_folder, filename)
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            downloaded.append({
                'original_url': img_url,
                'local_path': f"products/{product_id}/{filename}",
                'filename': filename
            })
            
        except Exception as e:
            failed.append({
                'url': img_url,
                'error': str(e)
            })
    
    return jsonify({
        'success': True,
        'downloaded': downloaded,
        'failed': failed,
        'count': len(downloaded)
    })


@main_bp.route('/api/products/<int:product_id>/assets')
@login_required
def get_product_assets(product_id):
    """Get list of locally stored assets for a product."""
    product = Product.query.get_or_404(product_id)
    
    product_folder = os.path.join(
        current_app.config['PRODUCT_UPLOAD_FOLDER'],
        str(product_id)
    )
    
    assets = {
        'images': [],
        'other': []
    }
    
    if os.path.exists(product_folder):
        for filename in os.listdir(product_folder):
            filepath = os.path.join(product_folder, filename)
            if os.path.isfile(filepath):
                ext = os.path.splitext(filename)[1].lower()
                asset_info = {
                    'filename': filename,
                    'path': f"products/{product_id}/{filename}",
                    'size': os.path.getsize(filepath),
                    'url': f"/uploads/products/{product_id}/{filename}"
                }
                
                if ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
                    assets['images'].append(asset_info)
                else:
                    assets['other'].append(asset_info)
    
    return jsonify({
        'product_id': product_id,
        'assets': assets
    })


@main_bp.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename):
    """Serve uploaded files with path traversal protection."""
    upload_folder = current_app.config['UPLOAD_FOLDER']
    
    # Security: Validate filename to prevent path traversal
    # 1. Use secure_filename to sanitize
    safe_filename = secure_filename(filename)
    
    # 2. Check if the safe path is still within upload folder
    if not is_safe_path(upload_folder, safe_filename):
        current_app.logger.warning(f'Path traversal attempt detected: {filename}')
        abort(403, 'Access denied')
    
    # 3. Additional check: ensure no '..' in path components
    if '..' in filename or filename.startswith('/'):
        current_app.logger.warning(f'Invalid path detected: {filename}')
        abort(403, 'Access denied')
    
    return send_from_directory(upload_folder, safe_filename)


# ============================================================================
# Spec Sheet Routes
# ============================================================================

@main_bp.route('/api/products/<int:product_id>/spec-sheet')
@login_required
def get_spec_sheet(product_id):
    """Generate a comprehensive spec sheet for a product."""
    product = Product.query.get_or_404(product_id)
    
    # Get local assets
    product_folder = os.path.join(
        current_app.config['PRODUCT_UPLOAD_FOLDER'],
        str(product_id)
    )
    
    local_images = []
    if os.path.exists(product_folder):
        for filename in sorted(os.listdir(product_folder)):
            ext = os.path.splitext(filename)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
                local_images.append({
                    'filename': filename,
                    'url': f"/uploads/products/{product_id}/{filename}",
                    'path': f"products/{product_id}/{filename}"
                })
    
    # Build spec sheet
    spec_sheet = {
        'product_id': product.id,
        'name': product.name,
        'url': product.url,
        'brand': product.brand,
        'description': product.description,
        'price': {
            'current': product.price,
            'currency': product.currency
        },
        'specifications': product.specifications or {},
        'reviews': {
            'count': len(product.reviews) if product.reviews else 0,
            'items': product.reviews or []
        },
        'assets': {
            'remote_images': product.images or [],
            'local_images': local_images,
            'total_images': len(product.images or []) + len(local_images)
        },
        'metadata': {
            'status': product.status,
            'created_at': product.created_at.isoformat() if product.created_at else None,
            'updated_at': product.updated_at.isoformat() if product.updated_at else None
        }
    }
    
    return jsonify(spec_sheet)


# ============================================================================
# Use Case Routes
# ============================================================================

@main_bp.route('/use-case/<int:product_id>')
@login_required
def use_case_page(product_id):
    """Use case configuration UI page."""
    product = Product.query.get_or_404(product_id)
    return render_template('use_case.html', product=product)


@main_bp.route('/api/products/<int:product_id>/use-cases', methods=['GET'])
@login_required
def get_use_cases(product_id):
    """Get all use cases for a product."""
    product = Product.query.get_or_404(product_id)
    use_cases = UseCase.query.filter_by(product_id=product_id).order_by(UseCase.created_at.desc()).all()
    return jsonify([uc.to_dict() for uc in use_cases])


@main_bp.route('/api/use-cases/<int:use_case_id>', methods=['GET'])
@login_required
def get_use_case(use_case_id):
    """Get a single use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    return jsonify(use_case.to_dict())


@main_bp.route('/api/products/<int:product_id>/use-cases', methods=['POST'])
@login_required
def create_use_case(product_id):
    """Create a new use case for a product."""
    product = Product.query.get_or_404(product_id)
    data = request.get_json()
    
    use_case = UseCase(
        product_id=product_id,
        name=data.get('name', 'New Use Case'),
        format=data.get('format', '9:16'),
        style=data.get('style', 'realistic'),
        goal=data.get('goal', ''),
        target_audience=data.get('target_audience', ''),
        duration_target=data.get('duration_target', 15),
        voice_id=data.get('voice_id', ''),
        voice_settings=data.get('voice_settings', {}),
        status='configured' if data.get('voice_id') else 'draft'
    )
    use_case.sync_num_clips()
    
    db.session.add(use_case)
    db.session.commit()
    
    return jsonify(use_case.to_dict()), 201


@main_bp.route('/api/use-cases/<int:use_case_id>', methods=['PUT'])
@login_required
def update_use_case(use_case_id):
    """Update a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    data = request.get_json()
    
    use_case.name = data.get('name', use_case.name)
    use_case.format = data.get('format', use_case.format)
    use_case.style = data.get('style', use_case.style)
    use_case.goal = data.get('goal', use_case.goal)
    use_case.target_audience = data.get('target_audience', use_case.target_audience)
    use_case.duration_target = data.get('duration_target', use_case.duration_target)
    use_case.voice_id = data.get('voice_id', use_case.voice_id)
    use_case.voice_settings = data.get('voice_settings', use_case.voice_settings)
    use_case.sync_num_clips()
    
    # Update status if voice is selected
    if use_case.voice_id and use_case.status == 'draft':
        use_case.status = 'configured'
    
    db.session.commit()
    return jsonify(use_case.to_dict())


@main_bp.route('/api/use-cases/<int:use_case_id>', methods=['DELETE'])
@login_required
def delete_use_case(use_case_id):
    """Delete a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    db.session.delete(use_case)
    db.session.commit()
    return jsonify({'message': 'Use case deleted'})


@main_bp.route('/api/use-cases/<int:use_case_id>/duplicate', methods=['POST'])
@login_required
def duplicate_use_case(use_case_id):
    """Duplicate an existing use case."""
    original = UseCase.query.get_or_404(use_case_id)
    
    new_use_case = UseCase(
        product_id=original.product_id,
        name=f"{original.name} (Copy)",
        format=original.format,
        style=original.style,
        goal=original.goal,
        target_audience=original.target_audience,
        duration_target=original.duration_target,
        voice_id=original.voice_id,
        voice_settings=original.voice_settings,
        num_clips=original.calculated_num_clips,
        status='configured'
    )
    new_use_case.sync_num_clips()
    
    db.session.add(new_use_case)
    db.session.commit()
    
    return jsonify(new_use_case.to_dict()), 201


# ============================================================================
# ElevenLabs Voice Routes
# ============================================================================

@main_bp.route('/api/voices', methods=['GET'])
@login_required
def get_voices():
    """Get available ElevenLabs voices."""
    api_key = current_app.config.get('ELEVENLABS_API_KEY')
    
    if not api_key:
        # Return default voice list if no API key
        return jsonify({
            'voices': [
                {'voice_id': 'XB0fDUnXU5powFXDhCwa', 'name': 'Charlotte', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'XrExE9yKIg1WjnnlVkGX', 'name': 'Matilda', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'pFZP5JQG7iQjIQuC4Bku', 'name': 'Lily', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'cgSgspJ2msm6clMCkdW9', 'name': 'Jessica', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'TX3AEvVoIzMeN6rKPMj1', 'name': 'Michael', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'flq6f7yk4E4fJM5XTYuZ', 'name': 'Michael (Deep)', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'pNInz6obpgDQGcFmaJgB', 'name': 'Adam', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'IKne3meq5aSn9XLyUdCD', 'name': 'Josh', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'CwhRBWXzGAHq8TQ4Fs17', 'name': 'Roger', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'N2lVS1w4EtoT3dr4eOWO', 'name': 'Callum', 'preview_url': None, 'category': 'premade'},
            ]
        })
    
    try:
        headers = {'xi-api-key': api_key}
        response = requests.get('https://api.elevenlabs.io/v1/voices', headers=headers, timeout=30)
        response.raise_for_status()
        return jsonify(response.json())
    except Exception as e:
        # Return default voice list if API call fails
        return jsonify({
            'voices': [
                {'voice_id': 'XB0fDUnXU5powFXDhCwa', 'name': 'Charlotte', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'XrExE9yKIg1WjnnlVkGX', 'name': 'Matilda', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'pFZP5JQG7iQjIQuC4Bku', 'name': 'Lily', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'cgSgspJ2msm6clMCkdW9', 'name': 'Jessica', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'TX3AEvVoIzMeN6rKPMj1', 'name': 'Michael', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'flq6f7yk4E4fJM5XTYuZ', 'name': 'Michael (Deep)', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'pNInz6obpgDQGcFmaJgB', 'name': 'Adam', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'IKne3meq5aSn9XLyUdCD', 'name': 'Josh', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'CwhRBWXzGAHq8TQ4Fs17', 'name': 'Roger', 'preview_url': None, 'category': 'premade'},
                {'voice_id': 'N2lVS1w4EtoT3dr4eOWO', 'name': 'Callum', 'preview_url': None, 'category': 'premade'},
            ]
        })


@main_bp.route('/api/voices/preview', methods=['POST'])
@login_required
def preview_voice():
    """Generate a voice preview using ElevenLabs."""
    data = request.get_json()
    voice_id = data.get('voice_id')
    text = data.get('text', 'Welcome to our product showcase! This is how your voiceover will sound.')
    
    if not voice_id:
        return jsonify({'error': 'Voice ID is required'}), 400
    
    api_key = current_app.config.get('ELEVENLABS_API_KEY')
    if not api_key:
        return jsonify({'error': 'ElevenLabs API key not configured'}), 500
    
    try:
        # Get voice settings
        voice_settings = data.get('voice_settings', {
            'stability': 0.5,
            'similarity_boost': 0.75,
            'style': 0.0,
            'use_speaker_boost': True
        })
        
        headers = {
            'xi-api-key': api_key,
            'Content-Type': 'application/json'
        }
        
        payload = {
            'text': text,
            'model_id': 'eleven_multilingual_v2',
            'voice_settings': voice_settings
        }
        
        response = requests.post(
            f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        
        # Save audio to temp file
        preview_id = str(uuid.uuid4())
        preview_folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'previews')
        os.makedirs(preview_folder, exist_ok=True)
        
        audio_path = os.path.join(preview_folder, f'{preview_id}.mp3')
        with open(audio_path, 'wb') as f:
            f.write(response.content)
        
        return jsonify({
            'success': True,
            'preview_url': f'/uploads/previews/{preview_id}.mp3',
            'duration_estimate': len(text.split()) * 0.5  # Rough estimate
        })
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            return jsonify({
                'error': 'ElevenLabs API key is invalid or expired. Please check your API key configuration.',
                'fallback': True
            }), 200
        return jsonify({'error': f'ElevenLabs API error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Script Generation Routes
# ============================================================================

@main_bp.route('/script/<int:use_case_id>')
@login_required
def script_page(use_case_id):
    """Script generation UI page."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    return render_template('script.html', use_case=use_case, product=product)


@main_bp.route('/api/use-cases/<int:use_case_id>/script', methods=['GET'])
@login_required
def get_script(use_case_id):
    """Get the script for a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    
    if not script:
        return jsonify({'error': 'No script found for this use case'}), 404
    
    return jsonify(script.to_dict())


@main_bp.route('/api/use-cases/<int:use_case_id>/script', methods=['POST'])
@login_required
def generate_script_route(use_case_id):
    """Generate a new script for a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    
    # Check for existing script
    existing_script = Script.query.filter_by(use_case_id=use_case_id).first()
    
    # Get OpenAI API key
    api_key = current_app.config.get('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'OpenAI API key not configured'}), 500
    
    try:
        # Prepare product data
        product_data = {
            'name': product.name,
            'description': product.description,
            'brand': product.brand,
            'price': product.price,
            'currency': product.currency,
            'specifications': product.specifications,
            'reviews': product.reviews
        }
        
        # Prepare use case config
        use_case_config = {
            'format': use_case.format,
            'style': use_case.style,
            'goal': use_case.goal,
            'target_audience': use_case.target_audience,
            'duration_target': use_case.duration_target
        }
        
        # Generate script
        generator = ScriptGenerator(api_key=api_key)
        result = generator.generate_script(product_data, use_case_config)
        
        if not result['success']:
            return jsonify({'error': result.get('error', 'Script generation failed')}), 500
        
        # Save or update script
        if existing_script:
            existing_script.content = result['content']
            existing_script.estimated_duration = result['estimated_duration']
            existing_script.tone = use_case.style
            existing_script.generation_prompt = result.get('generation_prompt', '')
            existing_script.status = 'generated'
            db.session.commit()
            script = existing_script
        else:
            script = Script(
                use_case_id=use_case_id,
                content=result['content'],
                estimated_duration=result['estimated_duration'],
                tone=use_case.style,
                generation_prompt=result.get('generation_prompt', ''),
                status='generated'
            )
            db.session.add(script)
            db.session.commit()
        
        # Update use case status
        if use_case.status == 'configured':
            use_case.status = 'generating'
            db.session.commit()
        
        return jsonify({
            'success': True,
            'script': script.to_dict(),
            'word_count': result.get('word_count', 0)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/script', methods=['PUT'])
@login_required
def update_script(use_case_id):
    """Update an existing script (manual editing)."""
    use_case = UseCase.query.get_or_404(use_case_id)
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    
    if not script:
        return jsonify({'error': 'No script found for this use case'}), 404
    
    data = request.get_json()
    script.content = data.get('content', script.content)
    
    # Recalculate estimated duration
    word_count = len(script.content.split())
    script.estimated_duration = int(word_count / 2.3)
    script.status = 'approved' if data.get('approve', False) else script.status
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'script': script.to_dict(),
        'word_count': word_count
    })


@main_bp.route('/api/use-cases/<int:use_case_id>/script/regenerate', methods=['POST'])
@login_required
def regenerate_script(use_case_id):
    """Regenerate a script with optional refinement."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    existing = Script.query.filter_by(use_case_id=use_case_id).first()
    
    api_key = current_app.config.get('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'OpenAI API key not configured'}), 500
    
    data = request.get_json() or {}
    refinement = data.get('refinement', '')
    
    try:
        product_data = {
            'name': product.name,
            'description': product.description,
            'brand': product.brand,
            'price': product.price,
            'currency': product.currency,
            'specifications': product.specifications,
            'reviews': product.reviews
        }
        
        use_case_config = {
            'format': use_case.format,
            'style': use_case.style,
            'goal': use_case.goal,
            'target_audience': use_case.target_audience,
            'duration_target': use_case.duration_target
        }
        
        generator = ScriptGenerator(api_key=api_key)
        
        if refinement and existing:
            # Refine existing script
            result = generator.refine_script(
                existing.content,
                refinement,
                product_data,
                use_case_config
            )
        else:
            # Generate fresh script
            existing_content = existing.content if existing else None
            result = generator.generate_script(
                product_data,
                use_case_config,
                existing_script=existing_content
            )
        
        if not result['success']:
            return jsonify({'error': result.get('error', 'Script generation failed')}), 500
        
        # Save or update script
        if existing:
            existing.content = result['content']
            existing.estimated_duration = result['estimated_duration']
            existing.tone = use_case.style
            existing.status = 'generated'
            db.session.commit()
            script = existing
        else:
            script = Script(
                use_case_id=use_case_id,
                content=result['content'],
                estimated_duration=result['estimated_duration'],
                tone=use_case.style,
                status='generated'
            )
            db.session.add(script)
            db.session.commit()
        
        return jsonify({
            'success': True,
            'script': script.to_dict(),
            'word_count': result.get('word_count', 0)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/script/approve', methods=['POST'])
@login_required
def approve_script(use_case_id):
    """Approve a script for video generation."""
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    
    if not script:
        return jsonify({'error': 'No script found'}), 404
    
    script.status = 'approved'
    db.session.commit()
    
    return jsonify({
        'success': True,
        'script': script.to_dict()
    })


@main_bp.route('/api/use-cases/<int:use_case_id>/script', methods=['DELETE'])
@login_required
def delete_script(use_case_id):
    """Delete a script."""
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    
    if not script:
        return jsonify({'error': 'No script found'}), 404
    
    db.session.delete(script)
    db.session.commit()
    
    return jsonify({'message': 'Script deleted'})


# ============================================================================
# Video Generation Routes
# ============================================================================

@main_bp.route('/video-gen/<int:use_case_id>')
@login_required
def video_gen_page(use_case_id):
    """Video generation UI page."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    return render_template('video_gen.html', use_case=use_case, product=product, script=script)


@main_bp.route('/api/video-models', methods=['GET'])
@login_required
def get_video_models():
    """Get available video generation models."""
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        client = PolloAIClient(api_key=api_key)
        models = client.get_available_models()
        return jsonify({'success': True, 'models': models})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/clips', methods=['GET'])
@login_required
def get_video_clips(use_case_id):
    """Get all video clips for a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        clips = manager.get_use_case_clips(use_case_id, refresh_status=True)
        stats = manager.get_generation_stats(use_case_id)

        if stats.get('is_complete') and use_case.status == 'generating':
            use_case.status = 'complete'
            db.session.commit()
        
        return jsonify({
            'success': True,
            'clips': clips,
            'stats': stats
        })
    except Exception as e:
        import traceback
        current_app.logger.error(f"Error in get_video_clips: {e}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()}), 500


@main_bp.route('/api/products/<int:product_id>/images', methods=['GET'])
@login_required
def get_product_images(product_id):
    """Get all product images for selection."""
    product = Product.query.get_or_404(product_id)
    upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
    
    product_folder = os.path.join(upload_folder, 'products', str(product_id))
    images = []
    
    if os.path.exists(product_folder):
        for filename in sorted(os.listdir(product_folder)):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                images.append({
                    'filename': filename,
                    'url': f"/uploads/products/{product_id}/{filename}"
                })
    
    return jsonify({
        'success': True,
        'images': images,
        'product_name': product.name
    })


@main_bp.route('/api/use-cases/<int:use_case_id>/generate-clips', methods=['POST'])
@login_required
def generate_video_clips(use_case_id):
    """Generate a SINGLE video clip for a use case with image selection."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    
    if not script:
        return jsonify({'error': 'No script found. Generate a script first.'}), 400
    
    if script.status != 'approved':
        return jsonify({'error': 'Script must be approved before generating videos.'}), 400
    
    try:
        data = request.get_json() or {}
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        # Get existing clips count
        existing_clips = VideoClip.query.filter_by(use_case_id=use_case_id).count()
        
        # Get selected image if provided (for UI only - we can't send local files to Pollo)
        selected_image = data.get('selected_image')
        
        # Note: Local image files can't be sent to Pollo.ai directly
        # They need to be publicly accessible URLs or base64 encoded
        # For now, we use the image selection as a reference for the user
        # but generate text-to-video with improved prompts
        product_desc = (product.description or product.name or 'product')[:100]
        prompts = [
            f"Elegant product showcase of {product.name}, {product_desc}, professional studio lighting, premium quality presentation",
            f"Beautiful close-up of {product.name} showing details, {product_desc}, smooth camera movement",
            f"Lifestyle scene featuring {product.name} in use, {product_desc}, warm inviting atmosphere"
        ]
        prompt = prompts[existing_clips % len(prompts)]
        
        # Create single clip
        clip = manager.create_clip(
            use_case_id=use_case_id,
            sequence_order=existing_clips,
            prompt=prompt,
            length=5
        )
        
        # Start generation (text-to-video only for now)
        result = manager.start_generation(clip.id)
        
        if result.get('success'):
            use_case.status = 'generating'
            db.session.commit()
            
            return jsonify({
                'success': True,
                'clip': clip.to_dict(),
                'message': f'Started generation of clip {existing_clips + 1}. Generate again for next clip.',
                'prompt_sent': prompt,
                'note': 'Image-to-video requires publicly hosted images. Using text-to-video with product details.'
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Failed to start generation'),
                'prompt_sent': prompt
            }), 500
        
    except Exception as e:
        import traceback
        current_app.logger.error(f"Error generating clip: {e}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/upload-video', methods=['POST'])
@login_required
def upload_video_clip(use_case_id):
    """Upload a user-provided video clip with path traversal protection."""
    use_case = UseCase.query.get_or_404(use_case_id)
    
    if 'video' not in request.files:
        return jsonify({'error': 'No video file provided'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    clip_type = request.form.get('clip_type', 'custom')
    
    try:
        # Get existing clips count for sequence order
        existing_clips = VideoClip.query.filter_by(use_case_id=use_case_id).count()
        
        # Create clip folder with safe path
        clip_folder = safe_join(
            current_app.config['CLIP_UPLOAD_FOLDER'],
            str(use_case_id)
        )
        if not clip_folder:
            return jsonify({'error': 'Invalid use case ID'}), 400
        os.makedirs(clip_folder, exist_ok=True)
        
        # Save the uploaded file with secure filename
        original_filename = secure_filename(file.filename)
        filename = f"uploaded_{existing_clips + 1:02d}_{original_filename}"
        filepath = os.path.join(clip_folder, filename)
        
        # Additional safety check
        if not is_safe_path(current_app.config['CLIP_UPLOAD_FOLDER'], filepath):
            return jsonify({'error': 'Invalid file path'}), 403
            
        file.save(filepath)
        
        # Get video duration using ffprobe or default to 5 seconds
        try:
            import subprocess
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True
            )
            duration = int(float(result.stdout.strip())) if result.returncode == 0 else 5
        except:
            duration = 5
        
        # Create clip record
        clip = VideoClip(
            use_case_id=use_case_id,
            sequence_order=existing_clips,
            prompt=f'User uploaded video - {clip_type}',
            model_used='uploaded',
            duration=duration,
            status='complete',
            file_path=f'clips/{use_case_id}/{filename}',
            completed_at=datetime.utcnow()
        )
        db.session.add(clip)
        db.session.commit()
        
        # Generate thumbnail
        try:
            manager = VideoClipManager(
                api_key=current_app.config.get('POLLO_API_KEY'),
                upload_folder=current_app.config['UPLOAD_FOLDER']
            )
            thumbnail_path = manager._generate_thumbnail(filepath, use_case_id, clip.id)
            if thumbnail_path:
                clip.thumbnail_path = thumbnail_path
                db.session.commit()
        except Exception as e:
            current_app.logger.warning(f"Failed to generate thumbnail: {e}")
        
        return jsonify({
            'success': True,
            'clip': clip.to_dict(),
            'message': 'Video uploaded successfully'
        })
        
    except Exception as e:
        import traceback
        current_app.logger.error(f"Error uploading video: {e}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/clips/<int:clip_id>/status', methods=['GET'])
@login_required
def check_clip_status(clip_id):
    """Check the status of a video clip generation."""
    clip = VideoClip.query.get_or_404(clip_id)
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        result = manager.check_clip_status(clip_id)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/clips/<int:clip_id>/regenerate', methods=['POST'])
@login_required
def regenerate_video_clip(clip_id):
    """Regenerate a video clip."""
    clip = VideoClip.query.get_or_404(clip_id)
    data = request.get_json() or {}
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        result = manager.regenerate_clip(
            clip_id=clip_id,
            new_prompt=data.get('prompt')
        )
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/test-generate', methods=['POST'])
@login_required
def test_generate_single_clip():
    """Generate a single test clip with visible prompt and image-to-video support."""
    data = request.get_json() or {}
    use_case_id = data.get('use_case_id')
    
    if not use_case_id:
        return jsonify({'error': 'use_case_id required'}), 400
    
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        # Generate a single prompt for hook/intro
        prompt = f"Beautiful product showcase, elegant presentation, professional studio lighting, premium quality, smooth camera movement"
        
        # Check if we have product images for image-to-video
        product_folder = os.path.join(upload_folder, 'products', str(product.id))
        image_url = None
        if os.path.exists(product_folder):
            images = [f for f in os.listdir(product_folder) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
            if images:
                image_url = f"/uploads/products/{product.id}/{images[0]}"
        
        # Create single clip
        clip = manager.create_clip(
            use_case_id=use_case_id,
            sequence_order=0,
            prompt=prompt,
            length=5
        )
        
        # Log what we're sending to Pollo
        current_app.logger.info(f"Test generate - Prompt: {prompt}")
        current_app.logger.info(f"Test generate - Image URL: {image_url}")
        
        # Start generation
        result = manager.start_generation(clip.id)
        
        return jsonify({
            'success': result.get('success'),
            'clip_id': clip.id,
            'prompt_sent_to_pollo': prompt,
            'image_url_used': image_url,
            'pollo_result': result,
            'message': 'Check Pollo.ai dashboard to test this prompt directly'
        })
        
    except Exception as e:
        import traceback
        current_app.logger.error(f"Test generate error: {e}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/clips/<int:clip_id>', methods=['DELETE'])
@login_required
def delete_video_clip(clip_id):
    """Delete a video clip."""
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        result = manager.delete_clip(clip_id)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/clips/reorder', methods=['PUT'])
@login_required
def reorder_video_clips(use_case_id):
    """Reorder video clips for a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    data = request.get_json()
    clip_orders = data.get('clip_orders', [])
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        result = manager.reorder_clips(use_case_id, clip_orders)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/clips/<int:clip_id>/update-prompt', methods=['PUT'])
@login_required
def update_clip_prompt(clip_id):
    """Update the prompt for a clip (before generation)."""
    clip = VideoClip.query.get_or_404(clip_id)
    data = request.get_json()
    
    if clip.status not in ['pending', 'error']:
        return jsonify({'error': 'Can only update prompts for pending or error clips'}), 400
    
    clip.prompt = data.get('prompt', clip.prompt)
    db.session.commit()
    
    return jsonify({
        'success': True,
        'clip': clip.to_dict()
    })


@main_bp.route('/api/use-cases/<int:use_case_id>/generation-stats', methods=['GET'])
@login_required
def get_generation_stats(use_case_id):
    """Get generation statistics for a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        stats = manager.get_generation_stats(use_case_id)
        
        # Update use case status if all clips are complete
        if stats['is_complete'] and use_case.status == 'generating':
            use_case.status = 'complete'
            db.session.commit()
        
        return jsonify({
            'success': True,
            'stats': stats
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/pollo-credits', methods=['GET'])
@login_required
def get_pollo_credits():
    """Get Pollo.ai credit balance."""
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        client = PolloAIClient(api_key=api_key)
        credits = client.get_credit_balance()
        return jsonify(credits)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# Assembly Routes
# ============================================================================

@main_bp.route('/assembly/<int:use_case_id>')
@login_required
def assembly_page(use_case_id):
    """Video assembly UI page."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    return render_template('assembly.html', use_case=use_case, product=product, script=script)


@main_bp.route('/output/<int:use_case_id>')
@login_required
def output_page(use_case_id):
    """Final output UI page."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    final_video = FinalVideo.query.filter_by(use_case_id=use_case_id).order_by(FinalVideo.created_at.desc()).first()
    return render_template('output.html', use_case=use_case, product=product, final_video=final_video)


@main_bp.route('/api/use-cases/<int:use_case_id>/analyze-clips', methods=['POST'])
@login_required
def analyze_clips(use_case_id):
    """Analyze all clips for a use case using Vision AI."""
    UseCase.query.get_or_404(use_case_id)
    data = request.get_json() or {}
    force = data.get('force', False)
    
    try:
        api_key = current_app.config.get('OPENAI_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        analyzer = ClipAnalyzer(api_key=api_key)
        
        result = analyzer.analyze_use_case_clips(use_case_id, upload_folder, force=force)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/optimize-sequence', methods=['POST'])
@login_required
def optimize_sequence(use_case_id):
    """Return a recommended sequence without applying it."""
    use_case = UseCase.query.get_or_404(use_case_id)
    
    clips = VideoClip.query.filter_by(
        use_case_id=use_case_id,
        status='complete'
    ).order_by(VideoClip.sequence_order).all()
    
    if not clips:
        return jsonify({'error': 'No complete clips found for optimization'}), 400
    
    try:
        engine = ClipOrderingEngine()
        result = engine.recommend_order(clips, use_case)
        result['duration_check'] = result.get('duration_summary')
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/apply-sequence', methods=['POST'])
@login_required
def apply_sequence(use_case_id):
    """Apply a provided sequence ordering payload."""
    UseCase.query.get_or_404(use_case_id)
    data = request.get_json() or {}
    sequence_order = data.get('sequence_order', [])
    
    if not sequence_order:
        return jsonify({'error': 'No sequence order provided'}), 400
    
    try:
        engine = ClipOrderingEngine()
        result = engine.apply_sequence(use_case_id, sequence_order)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/clips/reorder', methods=['POST'])
@login_required
@login_required
def reorder_clips(use_case_id):
    """Reorder clips with custom order (legacy endpoint)."""
    UseCase.query.get_or_404(use_case_id)
    data = request.get_json() or {}
    clip_orders = data.get('clip_orders', [])
    
    if not clip_orders:
        return jsonify({'error': 'No clip orders provided'}), 400
    
    try:
        for item in clip_orders:
            clip = VideoClip.query.filter_by(
                id=item['clip_id'],
                use_case_id=use_case_id
            ).first()
            
            if clip:
                clip.sequence_order = item['sequence_order']
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Clip order updated successfully'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/clip-order', methods=['PUT'])
@login_required
def update_clip_order(use_case_id):
    """New clip order endpoint for assembly UI."""
    UseCase.query.get_or_404(use_case_id)
    data = request.get_json() or {}
    ordered_ids = data.get('ordered_clip_ids')
    clip_orders = data.get('clip_orders')
    
    if ordered_ids:
        sequence_payload = [
            {
                'clip_id': int(clip_id),
                'sequence_order': index
            }
            for index, clip_id in enumerate(ordered_ids)
        ]
    elif clip_orders:
        sequence_payload = [
            {
                'clip_id': int(item['clip_id']),
                'sequence_order': int(item['sequence_order'])
            }
            for item in clip_orders
        ]
    else:
        return jsonify({'error': 'ordered_clip_ids or clip_orders required'}), 400
    
    try:
        engine = ClipOrderingEngine()
        result = engine.apply_sequence(use_case_id, sequence_payload)
        if not result.get('success'):
            return jsonify(result), 500
        
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        clips = manager.get_use_case_clips(use_case_id)
        
        return jsonify({
            'success': True,
            'message': 'Clip order saved',
            'clips': clips,
            'result': result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/auto-order', methods=['POST'])
@login_required
def auto_order_clips(use_case_id):
    """Run the ordering engine and immediately apply the recommendation."""
    use_case = UseCase.query.get_or_404(use_case_id)
    
    clips = VideoClip.query.filter_by(
        use_case_id=use_case_id,
        status='complete'
    ).order_by(VideoClip.sequence_order).all()
    
    if not clips:
        return jsonify({'error': 'No complete clips available for auto-ordering'}), 400
    
    try:
        engine = ClipOrderingEngine()
        recommendation = engine.recommend_order(clips, use_case)
        if not recommendation.get('success'):
            return jsonify(recommendation), 400
        
        sequence_payload = [
            {
                'clip_id': item['clip_id'],
                'sequence_order': item['sequence_order']
            }
            for item in recommendation['recommended_order']
        ]
        apply_result = engine.apply_sequence(use_case_id, sequence_payload)
        if not apply_result.get('success'):
            return jsonify(apply_result), 500
        
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        clips_response = manager.get_use_case_clips(use_case_id)
        
        return jsonify({
            'success': True,
            'recommendation': recommendation,
            'clips': clips_response,
            'applied': True
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/assembly', methods=['GET'])
@login_required
@main_bp.route('/api/use-cases/<int:use_case_id>/assembly-data', methods=['GET'])
@login_required
def get_assembly_data(use_case_id):
    """Get all data needed for assembly UI."""
    use_case = UseCase.query.get_or_404(use_case_id)
    product = Product.query.get_or_404(use_case.product_id)
    script = Script.query.filter_by(use_case_id=use_case_id).first()
    
    upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
    api_key = current_app.config.get('POLLO_API_KEY')
    manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
    
    clips = manager.get_use_case_clips(use_case_id)
    stats = manager.get_generation_stats(use_case_id)

    ordered_clips = sorted(clips, key=lambda clip: clip.get('sequence_order') or 0)
    clip_order = [clip.get('id') for clip in ordered_clips]
    total_duration = round(sum((clip.get('duration') or 0) for clip in ordered_clips), 2)
    target_duration = use_case.duration_target or 30
    variance = round(total_duration - target_duration, 2)

    if variance > 5:
        duration_status = 'warning'
        duration_message = f"+{variance}s over target"
    elif variance < -5:
        duration_status = 'info'
        duration_message = f"{abs(variance)}s under target"
    else:
        duration_status = 'success'
        duration_message = 'Within target range'

    duration_summary = {
        'current': total_duration,
        'target': target_duration,
        'variance': variance,
        'status': duration_status,
        'message': duration_message
    }

    analyzed_count = len([clip for clip in ordered_clips if clip.get('content_description')])
    
    return jsonify({
        'success': True,
        'use_case': use_case.to_dict(),
        'product': product.to_dict(),
        'script': script.to_dict() if script else None,
        'clips': ordered_clips,
        'stats': stats,
        'clip_order': clip_order,
        'duration_summary': duration_summary,
        'analysis_summary': {
            'total': len(ordered_clips),
            'complete': stats.get('complete', 0),
            'analyzed': analyzed_count
        },
        'assembly_ready': stats.get('is_complete', False) and analyzed_count > 0
    })


@main_bp.route('/api/use-cases/<int:use_case_id>/assemble', methods=['POST'])
@login_required
def assemble_final_video(use_case_id):
    """Trigger final video assembly (Phase 8)."""
    use_case = UseCase.query.get_or_404(use_case_id)
    script = Script.query.filter_by(use_case_id=use_case_id).first()

    if not script:
        return jsonify({'error': 'No script found for this use case'}), 400
    if script.status != 'approved':
        return jsonify({'error': 'Script must be approved before final assembly'}), 400

    completed_clips = VideoClip.query.filter_by(
        use_case_id=use_case_id,
        status='complete'
    ).count()
    if completed_clips == 0:
        return jsonify({'error': 'Complete clips are required before assembly'}), 400

    data = request.get_json() or {}
    transition = data.get('transition', 'cut')
    quality = data.get('quality', 'medium')
    background_music = data.get('background_music')
    force_voiceover = data.get('force_voiceover', False)
    include_voiceover = data.get('include_voiceover', True)
    transition_duration = float(data.get('transition_duration', 0.5))
    format_override = data.get('format')

    upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
    ffmpeg_path = current_app.config.get('FFMPEG_PATH', 'ffmpeg')

    voiceover_path = data.get('voiceover_path')
    if include_voiceover:
        try:
            generator = VoiceoverGenerator(
                api_key=current_app.config.get('ELEVENLABS_API_KEY'),
                upload_folder=upload_folder,
                ffmpeg_path=ffmpeg_path
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 500

        if not voiceover_path:
            voiceover_result = generator.generate_voiceover(
                use_case=use_case,
                script=script,
                force=force_voiceover,
                background_music=background_music
            )
            if not voiceover_result.get('success'):
                status_code = 500 if 'error' in voiceover_result else 400
                return jsonify(voiceover_result), status_code
            voiceover_path = voiceover_result['file_path']

    assembler = VideoAssembler(upload_folder=upload_folder, ffmpeg_path=ffmpeg_path)
    assembly_result = assembler.assemble_use_case(
        use_case=use_case,
        script=script,
        audio_relative_path=voiceover_path,
        transition=transition,
        quality=quality,
        format_override=format_override,
        transition_duration=transition_duration
    )

    if not assembly_result.get('success'):
        return jsonify(assembly_result), 500

    return jsonify(assembly_result)


@main_bp.route('/api/use-cases/<int:use_case_id>/final-video', methods=['GET'])
@login_required
def get_final_video_info(use_case_id):
    """Return metadata about the latest final video."""
    final_video = FinalVideo.query.filter_by(use_case_id=use_case_id).order_by(FinalVideo.created_at.desc()).first()
    if not final_video:
        return jsonify({'success': False, 'error': 'No final video found'}), 404

    data = final_video.to_dict()
    data['video_url'] = f"/uploads/{final_video.file_path}" if final_video.file_path else None
    data['thumbnail_url'] = f"/uploads/{final_video.thumbnail_path}" if final_video.thumbnail_path else None
    data['download_url'] = url_for('main.download_final_video', use_case_id=use_case_id)

    return jsonify({'success': True, 'final_video': data})


@main_bp.route('/api/use-cases/<int:use_case_id>/download')
@login_required
def download_final_video(use_case_id):
    """Download the latest final video file."""
    final_video = FinalVideo.query.filter_by(use_case_id=use_case_id).order_by(FinalVideo.created_at.desc()).first()
    if not final_video or not final_video.file_path:
        return jsonify({'error': 'Final video not found'}), 404

    upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
    full_path = os.path.join(upload_folder, final_video.file_path)
    if not os.path.exists(full_path):
        return jsonify({'error': 'Final video file is missing'}), 404

    directory, filename = os.path.split(full_path)
    return send_from_directory(directory, filename, as_attachment=True, download_name=filename)


@main_bp.route('/webhooks/pollo', methods=['POST'])
def pollo_webhook():
    """Handle Pollo.ai video generation webhook callbacks."""
    try:
        raw_body = request.get_data()
        payload = request.get_json(silent=True) or {}
        
        # Verify webhook signature if secret is configured
        secret = current_app.config.get('POLLO_WEBHOOK_SECRET', '')
        signature = request.headers.get('X-Webhook-Signature') or request.headers.get('x-webhook-signature')
        webhook_id = request.headers.get('X-Webhook-Id') or request.headers.get('x-webhook-id')
        webhook_timestamp = request.headers.get('X-Webhook-Timestamp') or request.headers.get('x-webhook-timestamp')
        
        # Log full details for debugging
        current_app.logger.info('Webhook received: id=%s timestamp=%s signature=%s body=%s', 
                                webhook_id, webhook_timestamp, signature, raw_body.decode('utf-8')[:200] if raw_body else None)
        
        # TEMPORARILY DISABLED: Signature verification needs debugging
        # if secret and signature:
        #     if not _verify_pollo_signature(secret, signature, raw_body, webhook_id, webhook_timestamp):
        #         current_app.logger.warning('Invalid signature for webhook %s', webhook_id)
        #         return jsonify({'error': 'Invalid signature'}), 401
        
        # Extract task ID and status from payload
        task_id = payload.get('taskId') or payload.get('task_id') or _extract_nested_value(payload, 'data.taskId')
        status = _extract_pollo_status(payload)
        
        if not task_id:
            return jsonify({'error': 'No task ID in payload'}), 400
        
        # Find the clip by task ID
        clip = VideoClip.query.filter_by(pollo_job_id=task_id).first()
        if not clip:
            return jsonify({'error': 'Clip not found for task ID'}), 404
        
        # Update clip based on status
        if status in ('completed', 'succeeded', 'success', 'done'):
            video_url = _extract_pollo_video_url(payload)
            if video_url:
                try:
                    assets = _download_clip_assets(clip, video_url)
                    clip.status = 'complete'
                    clip.file_path = assets['video']
                    clip.completed_at = datetime.utcnow()
                    clip.error_message = None
                    if assets.get('thumbnail'):
                        clip.thumbnail_path = assets['thumbnail']
                except Exception as download_error:
                    current_app.logger.exception('Failed to download Pollo clip %s', clip.id)
                    clip.status = 'error'
                    clip.error_message = f'Download failed: {download_error}'
            else:
                clip.status = 'error'
                clip.error_message = 'No video URL in completed payload'
        elif status in ('failed', 'error', 'cancelled'):
            clip.status = 'error'
            clip.error_message = _extract_nested_value(payload, 'error.message') or _extract_nested_value(payload, 'message') or 'Generation failed'
        else:
            # Still processing - update to generating if pending
            if clip.status == 'pending':
                clip.status = 'generating'
        
        db.session.commit()
        
        return jsonify({'success': True, 'clip_id': clip.id, 'status': clip.status}), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Pipeline Recovery Routes
# ============================================================================

@main_bp.route('/api/use-cases/<int:use_case_id>/pipeline-status', methods=['GET'])
@login_required
def get_pipeline_status(use_case_id):
    """Get the current pipeline progress status for a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    summary = PipelineProgressTracker.summarize(use_case)
    return jsonify({'success': True, 'pipeline': summary})


@main_bp.route('/api/use-cases/<int:use_case_id>/pipeline-recover', methods=['POST'])
@login_required
def recover_pipeline(use_case_id):
    """Attempt to recover/resume a stalled pipeline."""
    use_case = UseCase.query.get_or_404(use_case_id)
    data = request.get_json() or {}
    target_stage = data.get('target_stage')  # Optional: limit recovery to specific stage
    
    try:
        recovery_service = PipelineRecoveryService()
        result = recovery_service.resume(use_case, target_stage=target_stage)
        
        return jsonify({
            'success': result.get('success', False),
            'actions': result.get('actions', []),
            'errors': result.get('errors', []),
            'summary': result.get('summary')
        })
        
    except Exception as e:
        import traceback
        current_app.logger.error(f"Pipeline recovery failed: {e}\n{traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': f'Recovery failed: {str(e)}',
            'error_type': 'recovery_failed'
        }), 500


@main_bp.route('/api/use-cases/<int:use_case_id>/retry-failed-clips', methods=['POST'])
@login_required
def retry_failed_clips(use_case_id):
    """Retry all failed clips for a use case."""
    use_case = UseCase.query.get_or_404(use_case_id)
    
    try:
        api_key = current_app.config.get('POLLO_API_KEY')
        upload_folder = current_app.config.get('UPLOAD_FOLDER', './uploads')
        manager = VideoClipManager(api_key=api_key, upload_folder=upload_folder)
        
        # Find all failed clips
        failed_clips = VideoClip.query.filter_by(
            use_case_id=use_case_id,
            status='error'
        ).all()
        
        if not failed_clips:
            return jsonify({
                'success': True,
                'message': 'No failed clips to retry',
                'retried': 0
            })
        
        retried = []
        errors = []
        
        for clip in failed_clips:
            try:
                result = manager.regenerate_clip(clip.id)
                if result.get('success'):
                    retried.append({
                        'clip_id': clip.id,
                        'sequence_order': clip.sequence_order,
                        'status': 'restarted'
                    })
                else:
                    errors.append({
                        'clip_id': clip.id,
                        'error': result.get('error', 'Unknown error')
                    })
            except Exception as clip_error:
                errors.append({
                    'clip_id': clip.id,
                    'error': str(clip_error)
                })
        
        return jsonify({
            'success': len(errors) == 0,
            'message': f'Retried {len(retried)} failed clips',
            'retried': retried,
            'errors': errors,
            'total': len(failed_clips)
        })
        
    except Exception as e:
        import traceback
        current_app.logger.error(f"Retry failed clips error: {e}\n{traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': f'Failed to retry clips: {str(e)}',
            'error_type': 'retry_failed'
        }), 500
