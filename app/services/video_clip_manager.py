"""Video clip management service."""
import os
import shutil
import logging
import cv2
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image
from flask import current_app, request

from app import db
from app.models import VideoClip, UseCase, Product
from app.services.pollo_ai import PolloAIClient
from app.services.clip_prompt_generator import ClipPromptGenerator
from app.utils import ExternalAPIError, NonRetryableAPIError


class VideoClipManager:
    """Manager for video clip operations including generation, download, and metadata."""
    
    def __init__(self, api_key: Optional[str] = None, upload_folder: str = './uploads'):
        """Initialize the video clip manager.
        
        Args:
            api_key: Pollo.ai API key
            upload_folder: Base folder for uploads
        """
        self.api_key = api_key
        self._pollo_client: Optional[PolloAIClient] = None
        self.upload_folder = upload_folder
        self.clips_folder = os.path.join(upload_folder, 'clips')
        self._fallback_logger = logging.getLogger(self.__class__.__name__)
        
        # Ensure clips folder exists
        os.makedirs(self.clips_folder, exist_ok=True)

    def _get_logger(self) -> logging.Logger:
        try:
            if current_app:
                return current_app.logger
        except RuntimeError:
            pass
        return self._fallback_logger

    def _log(self, level: int, message: str, **context):
        logger = self._get_logger()
        if not logger:
            return
        if context:
            logger.log(level, "%s | %s", message, context)
        else:
            logger.log(level, message)

    def _log_info(self, message: str, **context):
        self._log(logging.INFO, message, **context)

    def _log_error(self, message: str, **context):
        self._log(logging.ERROR, message, **context)

    def _log_debug(self, message: str, **context):
        self._log(logging.DEBUG, message, **context)

    def _log_warning(self, message: str, **context):
        self._log(logging.WARNING, message, **context)

    def _build_webhook_url(self) -> Optional[str]:
        """Return the absolute webhook URL if available."""
        base_url = None
        try:
            if current_app:
                base_url = (
                    current_app.config.get('EXTERNAL_BASE_URL')
                    or current_app.config.get('APP_BASE_URL')
                )
        except RuntimeError:
            base_url = None
        
        if not base_url:
            base_url = (
                os.getenv('APP_BASE_URL')
                or os.getenv('PUBLIC_BASE_URL')
                or os.getenv('EXTERNAL_BASE_URL')
            )
        
        if not base_url:
            try:
                base_url = request.url_root  # type: ignore[attr-defined]
            except RuntimeError:
                base_url = None
        
        if base_url:
            return base_url.rstrip('/') + '/webhooks/pollo'
        return None
    
    def _ensure_public_url(self, url: Optional[str]) -> Optional[str]:
        """Convert relative URLs (like /uploads/...) into fully qualified URLs."""
        if not url:
            return None

        cleaned = url.strip()
        if not cleaned:
            return None

        if cleaned.startswith(('http://', 'https://')):
            return cleaned

        base_url = None
        try:
            if current_app:
                base_url = (
                    current_app.config.get('EXTERNAL_BASE_URL')
                    or current_app.config.get('APP_BASE_URL')
                    or current_app.config.get('PUBLIC_BASE_URL')
                )
        except RuntimeError:
            base_url = None

        if not base_url:
            base_url = (
                os.getenv('EXTERNAL_BASE_URL')
                or os.getenv('APP_BASE_URL')
                or os.getenv('PUBLIC_BASE_URL')
            )

        if not base_url:
            return None

        if cleaned.startswith('/'):
            return base_url.rstrip('/') + cleaned

        return f"{base_url.rstrip('/')}/{cleaned.lstrip('/')}"

    def _prepare_image_for_video(self, image_url: str, clip_id: int) -> Optional[str]:
        """Prepare an image for image-to-video generation.
        
        TEMPORARILY DISABLED: Background removal is causing worker timeouts on Render.
        Returns None to use the original image URL directly.
        
        Args:
            image_url: URL or path to the image
            clip_id: ID of the clip (for naming processed files)
            
        Returns:
            None (always uses original image to avoid timeouts)
        """
        self._log_info('Skipping image processing - using original image URL',
                      clip_id=clip_id, image_url=image_url[:80] if image_url else None)
        # Background removal disabled due to Render worker timeouts
        # TODO: Re-enable with Celery async processing or faster implementation
        return None
    
    def _detect_white_background(self, img: Image.Image) -> bool:
        """Detect if an image has a white/plain background.
        
        Args:
            img: PIL Image
            
        Returns:
            True if image has white/plain background (>80% white border)
        """
        try:
            import numpy as np
            
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                img_rgb = img.convert('RGB')
            else:
                img_rgb = img
            
            # Convert to numpy array
            img_array = np.array(img_rgb)
            
            # Get border pixels (edges of the image)
            h, w = img_array.shape[:2]
            border_pixels = np.concatenate([
                img_array[0, :, :],      # Top row
                img_array[-1, :, :],     # Bottom row
                img_array[:, 0, :],      # Left column
                img_array[:, -1, :]      # Right column
            ])
            
            # Calculate percentage of white/light pixels in border
            # White = 255, light gray threshold = 240
            white_threshold = 240
            white_pixels = np.all(border_pixels >= white_threshold, axis=1)
            white_percentage = np.sum(white_pixels) / len(border_pixels)
            
            return white_percentage > 0.80
            
        except Exception as e:
            self._log_error(f'Failed to detect white background: {e}')
            return False


    @property
    def pollo_client(self) -> PolloAIClient:
        """Lazy-initialize the Pollo client since some operations don't need it."""
        if self._pollo_client is None:
            self._pollo_client = PolloAIClient(api_key=self.api_key)
        return self._pollo_client
    
    def generate_clip_prompts(
        self,
        use_case: UseCase,
        script_content: str,
        product: Product,
        num_clips: Optional[int] = None,
        scene_context: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Generate video prompts for each clip using GPT-4o Vision.
        
        Uses AI to analyze product images and create context-aware prompts
        that match the script, use case, and clip's narrative role.
        
        Args:
            use_case: The use case configuration
            script_content: The voiceover script
            product: The product being featured
            num_clips: Number of clips to generate (defaults to use_case.num_clips)
            scene_context: Optional scene context to enhance prompts (e.g., 'on kitchen counter')
            
        Returns:
            List of clip configurations with AI-generated prompts
        """
        if num_clips is None:
            num_clips = use_case.num_clips or getattr(use_case, 'calculated_num_clips', None) or 4
        
        # Get product images
        product_images = self._get_product_images(product)
        
        # Use GPT-4o Vision-powered prompt generator
        # Don't pass Pollo API key - ClipPromptGenerator will use OPENAI_API_KEY from env
        generator = ClipPromptGenerator()
        
        try:
            clips_config = generator.generate_clip_prompts(
                product=product,
                use_case=use_case,
                script_content=script_content,
                product_images=product_images,
                num_clips=num_clips,
                scene_context=scene_context
            )
            
            self._log_info(f"Generated {len(clips_config)} AI-powered clip prompts", 
                          use_case_id=use_case.id,
                          product_name=product.name)
            
            return clips_config
            
        except Exception as e:
            self._log_error(f"AI prompt generation failed, falling back to templates: {e}",
                           use_case_id=use_case.id)
            # Fallback to original template-based generation
            return self._generate_template_prompts(use_case, script_content, product, num_clips)
    
    def _get_product_images(self, product: Product) -> List[str]:
        """Get list of product image URLs/paths.
        
        Prioritizes original public URLs (from scraping) over local paths,
        since public URLs work with Pollo.ai for image-to-video generation.
        """
        images = []
        
        # First, use the original public URLs from the database (from scraping)
        # These are URLs like https://cdn.shopify.com/... that Pollo.ai can access
        if product.images:
            if isinstance(product.images, list):
                images.extend(product.images)
            elif isinstance(product.images, dict):
                images.extend(product.images.values())
        
        # Filter to only include public URLs (http/https) for Pollo.ai
        # Local file paths won't work with Pollo's image-to-video API
        public_urls = [img for img in images if img.startswith(('http://', 'https://'))]
        
        if public_urls:
            return public_urls
        
        # If no public URLs, fall back to local paths (for GPT-4o Vision analysis only)
        # These won't work with Pollo.ai but can still be used for prompt generation
        product_folder = os.path.join(self.upload_folder, 'products', str(product.id))
        if os.path.exists(product_folder):
            folder_images = [f"products/{product.id}/{f}" 
                           for f in os.listdir(product_folder)
                           if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
            images.extend(folder_images)
        
        return images
    
    def _generate_template_prompts(
        self,
        use_case: UseCase,
        script_content: str,
        product: Product,
        num_clips: int
    ) -> List[Dict[str, Any]]:
        """Fallback: Generate prompts using templates (original method)."""
        clip_types = self._determine_clip_types(num_clips)
        
        clips_config = []
        for i, clip_type in enumerate(clip_types):
            prompt = self._build_prompt_for_clip(
                clip_type=clip_type,
                clip_index=i,
                total_clips=num_clips,
                use_case=use_case,
                product=product,
                script_content=script_content
            )
            
            clips_config.append({
                'sequence_order': i,
                'clip_type': clip_type,
                'prompt': prompt,
                'motion_direction': 'smooth camera movement',
                'mood': 'professional',
                'estimated_duration': 5
            })
        
        return clips_config
    
    def _determine_clip_types(self, num_clips: int) -> List[str]:
        """Determine the type of each clip in the sequence."""
        if num_clips == 1:
            return ['product_showcase']
        elif num_clips == 2:
            return ['hook', 'product_showcase']
        elif num_clips == 3:
            return ['hook', 'product_demo', 'cta']
        elif num_clips == 4:
            return ['hook', 'problem', 'solution', 'cta']
        elif num_clips == 5:
            return ['hook', 'problem', 'solution', 'benefits', 'cta']
        else:
            # For more clips, cycle through types
            base_types = ['hook', 'problem', 'solution', 'benefits', 'social_proof', 'cta']
            types = []
            for i in range(num_clips):
                if i == 0:
                    types.append('hook')
                elif i == num_clips - 1:
                    types.append('cta')
                else:
                    types.append(base_types[min(i, len(base_types) - 2)])
            return types
    
    def _build_prompt_for_clip(
        self,
        clip_type: str,
        clip_index: int,
        total_clips: int,
        use_case: UseCase,
        product: Product,
        script_content: str
    ) -> str:
        """Build a video generation prompt for a specific clip type.
        
        Uses the actual script content and product details to create contextual prompts.
        """
        
        style = use_case.style or 'realistic'
        product_name = product.name or 'Product'
        product_desc = product.description or ''
        
        # Extract relevant script segment for this clip
        script_segment = self._extract_script_segment(script_content, clip_type, clip_index, total_clips)
        
        # Style descriptors
        style_descriptors = {
            'realistic': 'photorealistic, high quality, professional lighting, 4K, detailed',
            'cinematic': 'cinematic, dramatic lighting, film grain, anamorphic lens, movie quality',
            'animated': '3D animation, vibrant colors, smooth motion, Pixar style, playful',
            'comic': 'comic book style, bold outlines, vibrant colors, dynamic composition'
        }
        
        style_desc = style_descriptors.get(style, style_descriptors['realistic'])
        
        # Build contextual prompt based on clip type AND script content
        prompts = {
            'hook': self._build_hook_prompt(product_name, product_desc, script_segment, style_desc),
            'problem': self._build_problem_prompt(product_name, product_desc, script_segment, style_desc),
            'solution': self._build_solution_prompt(product_name, product_desc, script_segment, style_desc),
            'product_showcase': self._build_showcase_prompt(product_name, product_desc, script_segment, style_desc),
            'product_demo': self._build_demo_prompt(product_name, product_desc, script_segment, style_desc),
            'benefits': self._build_benefits_prompt(product_name, product_desc, script_segment, style_desc),
            'social_proof': self._build_social_proof_prompt(product_name, product_desc, script_segment, style_desc),
            'cta': self._build_cta_prompt(product_name, product_desc, script_segment, style_desc)
        }
        
        base_prompt = prompts.get(clip_type, prompts['product_showcase'])
        
        # Add format-specific guidance
        format_guidance = {
            '9:16': 'vertical video format, mobile-friendly, portrait orientation',
            '16:9': 'horizontal video format, widescreen, cinematic landscape',
            '1:1': 'square video format, balanced composition, social media optimized',
            '4:5': 'vertical video format, portrait orientation, social media friendly'
        }
        
        format_desc = format_guidance.get(use_case.format, format_guidance['9:16'])
        
        final_prompt = f"{base_prompt}, {format_desc}, smooth professional camera work, high production value"
        
        self._log_info(f"Generated {clip_type} prompt for {product_name}", 
                      clip_index=clip_index, 
                      script_segment_preview=script_segment[:50] if script_segment else 'None')
        
        return final_prompt
    
    def _extract_script_segment(self, script_content: str, clip_type: str, clip_index: int, total_clips: int) -> str:
        """Extract the relevant portion of the script for this clip type."""
        if not script_content:
            return ""
        
        sentences = [s.strip() for s in script_content.replace('!', '.').replace('?', '.').split('.') if s.strip()]
        if not sentences:
            return script_content[:100]
        
        # Map clip position to script portion
        if total_clips == 1:
            return script_content
        
        # Distribute sentences across clips
        sentences_per_clip = max(1, len(sentences) // total_clips)
        start_idx = clip_index * sentences_per_clip
        end_idx = min(start_idx + sentences_per_clip + 1, len(sentences))
        
        segment = '. '.join(sentences[start_idx:end_idx])
        return segment if segment else script_content[:100]
    
    def _build_hook_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build hook clip prompt with script context."""
        context = f" related to: {script_segment}" if script_segment else ""
        return f"Eye-catching opening shot featuring {product_name}{context}. Dynamic camera movement, engaging visual that immediately grabs attention. The {product_name} prominently featured or implied. {style_desc}"
    
    def _build_problem_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build problem clip prompt with script context."""
        context = f" showing: {script_segment}" if script_segment else ""
        return f"Scene establishing the problem or pain point{context}. Relatable situation showing the 'before' state. Emotional connection to the struggle. {style_desc}, storytelling approach"
    
    def _build_solution_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build solution clip prompt with script context."""
        context = f" demonstrating: {script_segment}" if script_segment else ""
        product_context = f" About {product_name}: {product_desc[:100]}" if product_desc else ""
        return f"Beautiful demonstration of {product_name} solving the problem{context}.{product_context} The transformation moment. Elegant presentation, smooth camera movement. {style_desc}"
    
    def _build_showcase_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build product showcase prompt with script context."""
        context = f" while showing: {script_segment}" if script_segment else ""
        return f"Stunning highlight of {product_name}{context}. Multiple angles showing the product's best features. Premium quality, professional studio lighting. {product_name} looking elegant and desirable. {style_desc}"
    
    def _build_demo_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build demo clip prompt with script context."""
        context = f" demonstrating: {script_segment}" if script_segment else ""
        return f"Step by step demonstration of {product_name} in action{context}. Hands interacting with the product, showing how it works. Clear visibility of the functionality. {style_desc}"
    
    def _build_benefits_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build benefits clip prompt with script context."""
        context = f" showing: {script_segment}" if script_segment else ""
        return f"Lifestyle scene showing the satisfaction from using {product_name}{context}. Happy person enjoying the results, aspirational setting. The 'after' state. Warm atmosphere. {style_desc}"
    
    def _build_social_proof_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build social proof clip prompt with script context."""
        context = f" related to: {script_segment}" if script_segment else ""
        return f"Scene suggesting people enjoying {product_name}{context}. Positive atmosphere, community feeling. Visual social validation. {style_desc}"
    
    def _build_cta_prompt(self, product_name: str, product_desc: str, script_segment: str, style_desc: str) -> str:
        """Build CTA clip prompt with script context."""
        context = f" emphasizing: {script_segment}" if script_segment else ""
        return f"Strong closing scene with {product_name} front and center{context}. Memorable final image, clear view of the product. Call-to-action energy, urgency. {style_desc}"
    
    def create_clip(
        self,
        use_case_id: int,
        sequence_order: int,
        prompt: str,
        model: Optional[str] = None,
        length: int = 5
    ) -> VideoClip:
        """Create a new video clip record and start generation.
        
        Args:
            use_case_id: ID of the use case
            sequence_order: Position in the video sequence
            prompt: Video generation prompt
            model: Model to use (defaults to use case style preference)
            length: Video length in seconds
            
        Returns:
            VideoClip model instance
        """
        use_case = UseCase.query.get(use_case_id)
        if not use_case:
            raise ValueError(f"Use case {use_case_id} not found")
        
        # Get recommended model if not specified
        if not model:
            recommended = self.pollo_client.get_models_for_style(use_case.style or 'realistic')
            model = recommended[0] if recommended else 'pollo-1.6'
        
        # Create clip record
        clip = VideoClip(
            use_case_id=use_case_id,
            sequence_order=sequence_order,
            prompt=prompt,
            model_used=model,
            duration=length,
            status='pending'
        )
        
        db.session.add(clip)
        db.session.commit()
        
        return clip
    
    def start_generation(
        self,
        clip_id: int,
        image_url: Optional[str] = None,
        *,
        allow_auto_image: bool = True,
        image_source: Optional[str] = None
    ) -> Dict[str, Any]:
        """Start video generation for a clip.
        
        Args:
            clip_id: ID of the clip to generate
            image_url: Optional image URL for image-to-video generation
            
        Returns:
            Result of the generation job creation
        """
        clip = VideoClip.query.get(clip_id)
        if not clip:
            return {'success': False, 'error': 'Clip not found'}
        
        if clip.status == 'generating':
            return {'success': False, 'error': 'Clip is already being generated'}
        
        use_case = UseCase.query.get(clip.use_case_id)
        if not use_case:
            return {'success': False, 'error': 'Use case not found'}
        
        try:
            # Update status
            clip.status = 'generating'
            clip.error_message = None
            db.session.commit()
            
            # If no image_url provided, use the original public URLs from the product
            # These are the publicly accessible URLs from the original source (e.g., Shopify CDN)
            if not image_url:
                product = Product.query.get(use_case.product_id)
                if product and product.images:
                    # product.images contains the original public URLs from scraping
                    if isinstance(product.images, list) and len(product.images) > 0:
                        # Select image based on clip sequence order for variety
                        image_index = clip.sequence_order % len(product.images)
                        image_url = product.images[image_index]
                        self._log_info('Using public product image URL for image-to-video', 
                                     clip_id=clip.id, 
                                     image_url=image_url[:100] + '...' if len(image_url) > 100 else image_url)
            
            # Determine aspect ratio from use case format
            # Handle both None and empty string cases
            aspect_ratio = use_case.format if use_case.format and use_case.format.strip() else '9:16'
            self._log_info('Starting video generation',
                         clip_id=clip.id,
                         aspect_ratio=aspect_ratio,
                         use_case_format=use_case.format,
                         model=clip.model_used or 'kling-1.6')
            
            # Check if image has plain/white background - if so, remove it
            # Plain backgrounds cause the video to start with that background, creating bad transitions
            # Removing the background gives AI a clean product cutout to work with
            processed_image_url = None
            if image_url:
                processed_image_url = self._prepare_image_for_video(image_url, clip.id)
                if processed_image_url:
                    # Use the background-removed version (full public URL)
                    image_url = processed_image_url
                    self._log_info('Using background-removed image for video',
                                 clip_id=clip.id,
                                 image_url=image_url[:100] + '...' if len(image_url) > 100 else image_url)
            
            # Create the video generation job
            webhook_url = self._build_webhook_url()
            result = self.pollo_client.create_video_job(
                prompt=clip.prompt,
                model=clip.model_used or 'kling-1.6',
                aspect_ratio=aspect_ratio,
                length=int(clip.duration) if clip.duration else 5,
                image_url=image_url,
                webhook_url=webhook_url
            )
            
            # Check if the error is due to invalid image - retry without image
            if not result.get('success') and result.get('error'):
                error_msg = result.get('error', '').lower()
                if 'image pixel is invalid' in error_msg or 'invalid image' in error_msg:
                    self._log_info('Image rejected by Pollo, retrying with text-only generation',
                                 clip_id=clip.id,
                                 aspect_ratio=aspect_ratio,
                                 original_error=result.get('error'))
                    # Retry without image_url
                    result = self.pollo_client.create_video_job(
                        prompt=clip.prompt,
                        model=clip.model_used or 'kling-1.6',
                        aspect_ratio=aspect_ratio,
                        length=int(clip.duration) if clip.duration else 5,
                        image_url=None,  # Retry without image
                        webhook_url=webhook_url
                    )
                    if result.get('success'):
                        self._log_info('Successfully generated clip with text-only fallback',
                                     clip_id=clip.id,
                                     aspect_ratio=aspect_ratio)
            
            if result.get('success'):
                clip.pollo_job_id = result['task_id']
                db.session.commit()
                return result
            else:
                # Handle error from create_video_job
                error_msg = result.get('error', 'Unknown error from Pollo.ai')
                clip.status = 'error'
                clip.error_message = error_msg
                db.session.commit()
                return {
                    'success': False,
                    'error': error_msg,
                    'error_type': result.get('error_type', 'api_error')
                }
            
        except NonRetryableAPIError as e:
            # Non-retryable errors - mark clip as failed
            error_msg = str(e)
            self._log_error('Video generation failed (non-retryable)', clip_id=clip.id, error=error_msg)
            try:
                clip.status = 'error'
                clip.error_message = e.provider + ": " + error_msg
                db.session.commit()
            except Exception as db_err:
                db.session.rollback()
                self._log_error('Failed to update clip status after error', clip_id=clip.id, error=str(db_err))
            return {
                'success': False,
                'error': error_msg,
                'error_type': 'non_retryable',
                'provider': e.provider
            }
            
        except ExternalAPIError as e:
            # API errors that may be retryable
            error_msg = str(e)
            self._log_error('Video generation API error', clip_id=clip.id, error=error_msg)
            try:
                clip.status = 'error'
                clip.error_message = f"{e.provider}: {error_msg}"
                db.session.commit()
            except Exception as db_err:
                db.session.rollback()
                self._log_error('Failed to update clip status after API error', clip_id=clip.id, error=str(db_err))
            return {
                'success': False,
                'error': error_msg,
                'error_type': 'api_error',
                'provider': e.provider,
                'retryable': e.retryable
            }
            
        except Exception as e:
            import traceback
            error_details = f"{str(e)}\n{traceback.format_exc()}"
            self._log_error('Video generation failed unexpectedly', clip_id=clip.id, error=str(e), traceback=traceback.format_exc())
            
            # Rollback any pending DB changes
            try:
                db.session.rollback()
            except:
                pass
            
            # Update clip with error
            try:
                clip.status = 'error'
                clip.error_message = f"Unexpected error: {str(e)}"
                db.session.commit()
            except Exception as db_err:
                db.session.rollback()
                self._log_error('Failed to update clip status after exception', clip_id=clip.id, error=str(db_err))
            
            return {
                'success': False,
                'error': f"An unexpected error occurred: {str(e)}",
                'error_type': 'unexpected'
            }
    
    def _sync_clip_with_pollo(self, clip: VideoClip) -> Dict[str, Any]:
        """Sync a clip with the latest Pollo status without committing."""
        if not clip.pollo_job_id:
            self._log_debug('No pollo_job_id for clip', clip_id=clip.id)
            return {
                'success': True,
                'pollo_status': None,
                'result': None,
                'dirty': False
            }

        self._log_debug('Syncing clip with Pollo', clip_id=clip.id, pollo_job_id=clip.pollo_job_id, current_status=clip.status)
        
        status_result = self.pollo_client.check_job_status(clip.pollo_job_id, clip=clip)
        self._log_info('Got Pollo status for clip', clip_id=clip.id, status_result=status_result)
        
        if not status_result.get('success'):
            self._log_error('Pollo status check failed', clip_id=clip.id, error=status_result.get('error'))
            return {
                'success': False,
                'pollo_status': status_result.get('status'),
                'result': status_result.get('result'),
                'error': status_result.get('error'),
                'dirty': False
            }

        pollo_status = (status_result.get('status') or '').lower()
        dirty = False
        
        self._log_debug('Processing Pollo status', clip_id=clip.id, pollo_status=pollo_status, current_clip_status=clip.status)

        if pollo_status in ('succeed', 'completed', 'success'):
            if clip.status != 'complete':
                clip.status = 'complete'
                dirty = True
            if not clip.completed_at:
                clip.completed_at = datetime.utcnow()
                dirty = True

            video_url = self.pollo_client._extract_video_url(status_result.get('result'))
            if video_url and not clip.file_path:
                if self._download_clip_video(clip, video_url):
                    dirty = True
                else:
                    clip.status = 'error'
                    dirty = True
            elif not video_url and not clip.file_path:
                clip.status = 'error'
                clip.error_message = 'Pollo job completed without a video URL'
                dirty = True

        elif pollo_status in ('failed', 'error', 'cancelled'):
            # Extract error message from the nested result structure
            error_message = 'Generation failed'
            result_data = status_result.get('result', {})
            if isinstance(result_data, dict):
                data = result_data.get('data', {})
                if isinstance(data, dict):
                    generations = data.get('generations', [])
                    if isinstance(generations, list) and generations:
                        gen = generations[0]
                        if isinstance(gen, dict):
                            error_message = gen.get('failMsg') or gen.get('message') or error_message
                # Also check direct message in result
                if error_message == 'Generation failed':
                    error_message = result_data.get('message') or error_message
            # Fallback to top-level error
            if error_message == 'Generation failed':
                error_message = status_result.get('error') or error_message
            
            self._log_error('Pollo job failed', clip_id=clip.id, error_message=error_message, raw_result=result_data)
            if clip.status != 'error' or clip.error_message != error_message:
                clip.status = 'error'
                clip.error_message = error_message
                dirty = True
        else:
            # Still processing - update status from pending to generating if needed
            if clip.status == 'pending':
                self._log_debug('Setting clip to generating', clip_id=clip.id, pollo_status=pollo_status)
                clip.status = 'generating'
                dirty = True

        self._log_info('Clip sync complete', clip_id=clip.id, new_status=clip.status, dirty=dirty)
        
        return {
            'success': True,
            'pollo_status': pollo_status or None,
            'result': status_result.get('result'),
            'dirty': dirty
        }

    def refresh_generating_clips(self, clips: List[VideoClip]) -> Dict[int, Dict[str, Any]]:
        """Refresh all generating/pending clips with Pollo status.
        
        Also marks clips as error if they've been generating for too long (timeout).
        """
        status_map: Dict[int, Dict[str, Any]] = {}
        dirty = False
        
        # Timeout for stuck generating clips (30 minutes)
        GENERATION_TIMEOUT_MINUTES = 30
        timeout_threshold = datetime.utcnow() - timedelta(minutes=GENERATION_TIMEOUT_MINUTES)

        for clip in clips:
            if clip.status in ('generating', 'pending'):
                # Check if clip has been generating for too long
                if clip.status == 'generating' and clip.created_at:
                    if clip.created_at < timeout_threshold:
                        self._log_error('Clip generation timed out', 
                                       clip_id=clip.id, 
                                       created_at=clip.created_at.isoformat(),
                                       timeout_minutes=GENERATION_TIMEOUT_MINUTES)
                        clip.status = 'error'
                        clip.error_message = f'Generation timed out after {GENERATION_TIMEOUT_MINUTES} minutes'
                        dirty = True
                        status_map[clip.id] = {
                            'success': False,
                            'error': 'Generation timed out',
                            'pollo_status': 'timeout',
                            'dirty': True
                        }
                        continue
                
                # Only sync with Pollo if we have a job ID
                if clip.pollo_job_id:
                    sync_result = self._sync_clip_with_pollo(clip)
                    status_map[clip.id] = sync_result
                    if sync_result.get('dirty'):
                        dirty = True
                else:
                    # No pollo_job_id but clip is generating/pending - mark as error
                    if clip.status in ('generating', 'pending'):
                        self._log_error('Clip has no Pollo job ID', clip_id=clip.id)
                        clip.status = 'error'
                        clip.error_message = 'No generation job found (may have been lost)'
                        dirty = True
                        status_map[clip.id] = {
                            'success': False,
                            'error': 'No Pollo job ID',
                            'pollo_status': 'missing_job_id',
                            'dirty': True
                        }

        if dirty:
            db.session.commit()

        return status_map

    def check_clip_status(self, clip_id: int) -> Dict[str, Any]:
        """Check the generation status of a clip.
        
        Args:
            clip_id: ID of the clip to check
            
        Returns:
            Status information
        """
        clip = VideoClip.query.get(clip_id)
        if not clip:
            return {'success': False, 'error': 'Clip not found'}

        sync_result = self._sync_clip_with_pollo(clip)
        if sync_result.get('dirty'):
            db.session.commit()

        response = {
            'success': sync_result.get('success', True),
            'clip_id': clip_id,
            'status': clip.status,
            'pollo_status': sync_result.get('pollo_status'),
            'result': sync_result.get('result')
        }

        if not sync_result.get('success'):
            response['error'] = sync_result.get('error')

        return response
    
    def _download_clip_video(self, clip: VideoClip, video_url: str) -> bool:
        """Download the generated video for a clip.
        
        Args:
            clip: VideoClip instance
            video_url: URL of the video to download
            
        Returns:
            True if successful
        """
        try:
            # Create folder for this use case's clips
            clip_folder = os.path.join(self.clips_folder, str(clip.use_case_id))
            os.makedirs(clip_folder, exist_ok=True)
            
            # Download the video
            filename = f"clip_{clip.id:03d}_{clip.sequence_order:02d}.mp4"
            filepath = os.path.join(clip_folder, filename)
            
            self._log_info('Downloading video', clip_id=clip.id, url=video_url[:100], filepath=filepath)
            
            response = requests.get(video_url, stream=True, timeout=120)
            response.raise_for_status()
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            self._log_info('Video downloaded successfully', clip_id=clip.id, filepath=filepath, size=os.path.getsize(filepath))
            
            # Update clip record
            clip.file_path = f"clips/{clip.use_case_id}/{filename}"
            
            # Generate thumbnail
            thumbnail_path = self._generate_thumbnail(filepath, clip.use_case_id, clip.id)
            if thumbnail_path:
                clip.thumbnail_path = thumbnail_path
            
            return True
            
        except Exception as e:
            import traceback
            self._log_error('Error downloading clip video', clip_id=clip.id, error=str(e), traceback=traceback.format_exc())
            clip.error_message = f"Download error: {str(e)}"
            return False
    
    def _generate_thumbnail(
        self,
        video_path: str,
        use_case_id: int,
        clip_id: int
    ) -> Optional[str]:
        """Generate a thumbnail from a video.
        
        Args:
            video_path: Path to the video file
            use_case_id: Use case ID for folder organization
            clip_id: Clip ID for filename
            
        Returns:
            Relative path to the thumbnail, or None if failed
        """
        try:
            # Open video
            cap = cv2.VideoCapture(video_path)
            
            if not cap.isOpened():
                return None
            
            # Get total frames
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            
            # Seek to middle frame
            target_frame = total_frames // 2
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            
            # Read frame
            ret, frame = cap.read()
            cap.release()
            
            if not ret:
                return None
            
            # Create thumbnail folder
            thumb_folder = os.path.join(self.clips_folder, str(use_case_id), 'thumbnails')
            os.makedirs(thumb_folder, exist_ok=True)
            
            # Save thumbnail
            thumb_filename = f"clip_{clip_id:03d}_thumb.jpg"
            thumb_path = os.path.join(thumb_folder, thumb_filename)
            
            # Resize for thumbnail (max 480px width)
            height, width = frame.shape[:2]
            max_width = 480
            if width > max_width:
                ratio = max_width / width
                new_width = max_width
                new_height = int(height * ratio)
                frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
            
            cv2.imwrite(thumb_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            
            return f"clips/{use_case_id}/thumbnails/{thumb_filename}"
            
        except Exception as e:
            print(f"Error generating thumbnail: {e}")
            return None
    
    def regenerate_clip(self, clip_id: int, new_prompt: Optional[str] = None) -> Dict[str, Any]:
        """Regenerate a clip with optional new prompt.
        
        Args:
            clip_id: ID of the clip to regenerate
            new_prompt: Optional new prompt (uses existing if not provided)
            
        Returns:
            Result of the new generation job
        """
        clip = VideoClip.query.get(clip_id)
        if not clip:
            return {'success': False, 'error': 'Clip not found', 'error_type': 'not_found'}
        
        try:
            # Update prompt if provided
            if new_prompt:
                clip.prompt = new_prompt
            
            # Reset status
            clip.status = 'pending'
            clip.pollo_job_id = None
            clip.error_message = None
            clip.completed_at = None
            
            # Delete old files if they exist
            if clip.file_path:
                old_path = os.path.join(self.upload_folder, clip.file_path)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except OSError as e:
                        self._log_error('Failed to delete old video file', clip_id=clip_id, error=str(e))
                clip.file_path = None
            
            if clip.thumbnail_path:
                old_thumb = os.path.join(self.upload_folder, clip.thumbnail_path)
                if os.path.exists(old_thumb):
                    try:
                        os.remove(old_thumb)
                    except OSError as e:
                        self._log_error('Failed to delete old thumbnail', clip_id=clip_id, error=str(e))
                clip.thumbnail_path = None
            
            db.session.commit()
            
            # Start new generation
            return self.start_generation(clip_id)
            
        except Exception as e:
            db.session.rollback()
            self._log_error('Failed to regenerate clip', clip_id=clip_id, error=str(e))
            return {
                'success': False,
                'error': f'Failed to prepare clip for regeneration: {str(e)}',
                'error_type': 'regeneration_failed'
            }
    
    def get_use_case_clips(self, use_case_id: int, refresh_status: bool = False) -> List[Dict[str, Any]]:
        """Get all clips for a use case with full details.
        
        Args:
            use_case_id: ID of the use case
            refresh_status: Whether to refresh Pollo status for generating clips
            
        Returns:
            List of clip dictionaries
        """
        clips = VideoClip.query.filter_by(use_case_id=use_case_id).order_by(VideoClip.sequence_order).all()
        if refresh_status:
            self.refresh_generating_clips(clips)
        return [self._enrich_clip_data(clip) for clip in clips]
    
    def _enrich_clip_data(self, clip: VideoClip) -> Dict[str, Any]:
        """Enrich clip data with additional information."""
        data = clip.to_dict()
        
        # Add file URLs if they exist
        if clip.file_path:
            data['video_url'] = f"/uploads/{clip.file_path}"
        else:
            data['video_url'] = None
        
        if clip.thumbnail_path:
            # Verify the thumbnail file actually exists before returning URL
            thumb_full_path = os.path.join(self.upload_folder, clip.thumbnail_path)
            if os.path.exists(thumb_full_path):
                data['thumbnail_url'] = f"/uploads/{clip.thumbnail_path}"
            else:
                data['thumbnail_url'] = None
        else:
            data['thumbnail_url'] = None
        
        # Add file size if video exists
        if clip.file_path:
            full_path = os.path.join(self.upload_folder, clip.file_path)
            if os.path.exists(full_path):
                data['file_size'] = os.path.getsize(full_path)
                data['file_size_human'] = self._format_file_size(data['file_size'])
            else:
                data['file_size'] = 0
                data['file_size_human'] = '0 B'
        
        return data
    
    def _format_file_size(self, size_bytes: int) -> str:
        """Format file size in human readable format."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"
    
    def delete_clip(self, clip_id: int) -> Dict[str, Any]:
        """Delete a clip and its associated files.
        
        Args:
            clip_id: ID of the clip to delete
            
        Returns:
            Result of the deletion
        """
        clip = VideoClip.query.get(clip_id)
        if not clip:
            return {'success': False, 'error': 'Clip not found'}
        
        try:
            # Delete video file
            if clip.file_path:
                video_path = os.path.join(self.upload_folder, clip.file_path)
                if os.path.exists(video_path):
                    os.remove(video_path)
            
            # Delete thumbnail
            if clip.thumbnail_path:
                thumb_path = os.path.join(self.upload_folder, clip.thumbnail_path)
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
            
            # Delete from database
            db.session.delete(clip)
            db.session.commit()
            
            return {'success': True, 'message': 'Clip deleted'}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def reorder_clips(self, use_case_id: int, clip_orders: List[Dict[str, int]]) -> Dict[str, Any]:
        """Reorder clips for a use case.
        
        Args:
            use_case_id: ID of the use case
            clip_orders: List of {'clip_id': int, 'sequence_order': int}
            
        Returns:
            Result of the reordering
        """
        try:
            for item in clip_orders:
                clip = VideoClip.query.filter_by(
                    id=item['clip_id'],
                    use_case_id=use_case_id
                ).first()
                
                if clip:
                    clip.sequence_order = item['sequence_order']
            
            db.session.commit()
            return {'success': True, 'message': 'Clips reordered'}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def get_generation_stats(self, use_case_id: int) -> Dict[str, Any]:
        """Get generation statistics for a use case.
        
        Args:
            use_case_id: ID of the use case
            
        Returns:
            Statistics dictionary
        """
        clips = VideoClip.query.filter_by(use_case_id=use_case_id).all()
        
        total = len(clips)
        complete = sum(1 for c in clips if c.status == 'complete')
        generating = sum(1 for c in clips if c.status == 'generating')
        pending = sum(1 for c in clips if c.status == 'pending')
        error = sum(1 for c in clips if c.status == 'error')
        
        total_duration = sum(c.duration or 0 for c in clips if c.status == 'complete')
        
        return {
            'total_clips': total,
            'complete': complete,
            'generating': generating,
            'pending': pending,
            'error': error,
            'progress_percentage': round((complete / total * 100), 1) if total > 0 else 0,
            'total_duration': total_duration,
            'is_complete': complete == total and total > 0
        }
