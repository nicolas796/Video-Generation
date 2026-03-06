"""GPT-4o Vision-powered clip prompt generator for context-aware video generation."""
import os
import base64
import time
import json
import logging
from typing import Dict, Any, Optional, List
from pathlib import Path

import httpx
from openai import OpenAI

from app.utils import api_retry


class ClipPromptGenerator:
    """Generate context-aware video prompts using GPT-4o Vision.
    
    Analyzes product images, understands narrative structure, and creates
    tailored prompts for each clip based on its role in the storyline.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize the clip prompt generator.
        
        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
        """
        self.api_key = api_key or os.getenv('OPENAI_API_KEY')
        self.client: Optional[OpenAI] = None
        self._last_api_call = 0  # For rate limiting
        
        if self.api_key:
            # Use shorter timeout to avoid gunicorn worker timeout (30s limit)
            # 15s timeout leaves room for other processing
            http_client = httpx.Client(timeout=15.0, follow_redirects=True)
            self.client = OpenAI(api_key=self.api_key, http_client=http_client)
        
        self._logger = logging.getLogger(self.__class__.__name__)
    
    def _rate_limit(self):
        """Enforce 150ms delay between API calls (400 RPM max, under 500 RPM limit)."""
        elapsed = time.time() - self._last_api_call
        if elapsed < 0.15:
            time.sleep(0.15 - elapsed)
        self._last_api_call = time.time()
    
    def generate_clip_prompts(
        self,
        product: Any,  # Product model
        use_case: Any,  # UseCase model
        script_content: str,
        product_images: List[str],
        num_clips: Optional[int] = None,
        scene_context: Optional[str] = None,
        generation_mode: str = 'balanced'
    ) -> List[Dict[str, Any]]:
        """Generate video prompts for each clip using GPT-4o Vision.
        
        Args:
            product: The Product model instance
            use_case: The UseCase model instance
            script_content: The full voiceover script
            product_images: List of image file paths for the product
            num_clips: Number of clips to generate (defaults to use_case.num_clips)
            scene_context: Optional scene context to enhance prompts (e.g., 'on kitchen counter')
            
        Returns:
            List of clip configurations with AI-generated prompts
        """
        if num_clips is None:
            num_clips = use_case.num_clips or 4
        
        # Store scene context for use in prompt generation
        self._scene_context = scene_context
        
        # Determine clip types based on narrative structure
        clip_types = self._determine_clip_types(num_clips)
        
        # Split script into segments for each clip
        script_segments = self._segment_script(script_content, num_clips)
        
        clips_config = []
        
        # TEMPORARILY DISABLED: AI prompt generation causes timeouts on Render
        # Each API call takes 10-15s, and with multiple clips we exceed 30s gunicorn limit
        # TODO: Re-enable with async parallel calls or background processing
        use_ai = False  # Set to True to re-enable AI prompts
        
        for i, clip_type in enumerate(clip_types):
            # Select the best product image for this clip type
            image_path = self._select_image_for_clip(product_images, clip_type, i)
            generation_strategy = self._assign_generation_strategy(clip_type, generation_mode)
            model_choice = self._select_model_for_strategy(generation_strategy, getattr(use_case, 'style', 'realistic'))
            script_segment = script_segments[i] if i < len(script_segments) else ""
            
            if use_ai and self.client:
                # Generate AI-powered prompt (DISABLED - too slow on Render)
                prompt_data = self._generate_ai_prompt(
                    product=product,
                    use_case=use_case,
                    clip_type=clip_type,
                    clip_index=i,
                    total_clips=num_clips,
                    script_segment=script_segment,
                    full_script=script_content,
                    image_path=image_path
                )
            else:
                # Use fast template-based prompts
                prompt_data = self._fallback_prompt(
                    product,
                    use_case,
                    clip_type,
                    script_segment,
                    generation_strategy=generation_strategy
                )
            
            clips_config.append({
                'sequence_order': i,
                'clip_type': clip_type,
                'prompt': prompt_data.get('visual_prompt', ''),
                'motion_direction': prompt_data.get('motion_direction', ''),
                'mood': prompt_data.get('mood', ''),
                'estimated_duration': 5,
                'image_path': image_path,
                'generation_strategy': generation_strategy,
                'model_choice': model_choice,
                'script_segment': script_segment,
                'use_image': generation_strategy != 'world_model_broll',
                'prompt_components': {
                    'motion_prompt': prompt_data.get('motion_direction', ''),
                    'camera_prompt': prompt_data.get('camera_prompt', ''),
                    'atmosphere_prompt': prompt_data.get('atmosphere_prompt', ''),
                }
            })
        
        return clips_config

    def _assign_generation_strategy(self, clip_type: str, generation_mode: str = 'balanced') -> str:
        """Assign a clip generation strategy based on clip role and UX mode."""
        mode = (generation_mode or 'balanced').lower()

        if mode == 'product_accuracy':
            return 'composite_then_kling'
        if mode == 'creative_storytelling':
            if clip_type in {'problem', 'benefits', 'social_proof'}:
                return 'world_model_broll'
            return 'kling_product_locked'

        # balanced (default)
        if clip_type in {'problem', 'benefits', 'social_proof'}:
            return 'world_model_broll'
        if clip_type in {'product_demo', 'product_showcase'}:
            return 'kling_product_locked'
        return 'composite_then_kling'

    def _select_model_for_strategy(self, strategy: str, style: str) -> str:
        """Pick a default model for a strategy with safe fallbacks."""
        if strategy == 'world_model_broll':
            if style in {'cinematic', 'realistic'}:
                return 'sora-2'
            return 'veo-2'
        if strategy == 'composite_then_kling':
            return 'kling-2.1'
        return 'kling-1.6'
    
    def _determine_clip_types(self, num_clips: int) -> List[str]:
        """Determine the narrative type of each clip in the sequence."""
        if num_clips == 1:
            return ['product_showcase']
        elif num_clips == 2:
            return ['hook', 'cta']
        elif num_clips == 3:
            return ['hook', 'solution', 'cta']
        elif num_clips == 4:
            return ['hook', 'problem', 'solution', 'cta']
        elif num_clips == 5:
            return ['hook', 'problem', 'solution', 'benefits', 'cta']
        else:
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
    
    def _segment_script(self, script_content: str, num_clips: int) -> List[str]:
        """Split the script into segments aligned with clip count."""
        if not script_content or num_clips <= 1:
            return [script_content] if script_content else [""]
        
        # Split by sentence-ending punctuation
        import re
        sentences = re.split(r'[.!?]+', script_content)
        sentences = [s.strip() for s in sentences if s.strip()]
        
        if len(sentences) < num_clips:
            # Not enough sentences - repeat some
            while len(sentences) < num_clips:
                sentences.extend(sentences[:num_clips - len(sentences)])
        
        # Distribute sentences across clips
        segments = []
        sentences_per_clip = len(sentences) // num_clips
        remainder = len(sentences) % num_clips
        
        start = 0
        for i in range(num_clips):
            count = sentences_per_clip + (1 if i < remainder else 0)
            end = start + count
            segment = '. '.join(sentences[start:end])
            if segment and not segment.endswith('.'):
                segment += '.'
            segments.append(segment)
            start = end
        
        return segments
    
    def _select_image_for_clip(self, product_images: List[str], clip_type: str, clip_index: int) -> Optional[str]:
        """Select the most appropriate product image for a clip.
        
        Args:
            product_images: List of image URLs (can be local paths or public URLs)
            clip_type: Type of clip (hook, problem, solution, cta, etc.)
            clip_index: Index of the clip in the sequence
            
        Returns:
            Selected image URL or path
        """
        if not product_images:
            return None
        
        # Filter to prefer public URLs (http/https) for GPT-4o Vision
        # Public URLs work better for image analysis than local paths
        public_urls = [img for img in product_images if img.startswith(('http://', 'https://'))]
        
        # Use public URLs if available, otherwise fall back to all images
        images_to_use = public_urls if public_urls else product_images
        
        if not images_to_use:
            return None
        
        # Select based on clip type and index
        if clip_type == 'hook' and len(images_to_use) > 1:
            return images_to_use[0]  # First image often best for hook
        elif clip_type == 'cta' and len(images_to_use) > 1:
            return images_to_use[-1]  # Last image often best for CTA
        else:
            return images_to_use[clip_index % len(images_to_use)]
    
    def _encode_image(self, image_path: str) -> Optional[str]:
        """Encode an image to base64 for GPT-4o Vision."""
        try:
            # Handle relative paths
            if not os.path.isabs(image_path):
                # Try to find in upload folder
                base_path = os.getenv('UPLOAD_FOLDER', './uploads')
                full_path = os.path.join(base_path, image_path)
                if not os.path.exists(full_path):
                    full_path = image_path
            else:
                full_path = image_path
            
            if not os.path.exists(full_path):
                self._logger.warning(f"Image not found: {full_path}")
                return None
            
            with open(full_path, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            self._logger.error(f"Failed to encode image: {e}")
            return None
    
    def _generate_ai_prompt(
        self,
        product: Any,
        use_case: Any,
        clip_type: str,
        clip_index: int,
        total_clips: int,
        script_segment: str,
        full_script: str,
        image_path: Optional[str]
    ) -> Dict[str, str]:
        """Generate an AI-powered prompt using GPT-4o Vision.
        
        Returns dict with: visual_prompt, motion_direction, mood
        """
        if not self.client:
            # Fallback to template-based generation
            return self._fallback_prompt(product, use_case, clip_type, script_segment)
        
        try:
            self._rate_limit()  # Enforce rate limiting
            
            # Build the prompt
            system_prompt = self._build_system_prompt()
            user_content = self._build_user_content(
                product=product,
                use_case=use_case,
                clip_type=clip_type,
                clip_index=clip_index,
                total_clips=total_clips,
                script_segment=script_segment,
                full_script=full_script,
                image_path=image_path
            )
            
            # Make the API call with timeout protection
            import httpx
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.8,
                max_tokens=800,
                response_format={"type": "json_object"},
                timeout=15  # 15 second timeout for this specific call
            )
            
            # Parse the response
            content = response.choices[0].message.content
            result = json.loads(content)
            
            return {
                'visual_prompt': result.get('visual_prompt', ''),
                'motion_direction': result.get('motion_direction', ''),
                'mood': result.get('mood', '')
            }
                
        except httpx.TimeoutException as te:
            self._logger.warning(f"GPT-4o API call timed out after 15s, using fallback: {te}")
            return self._fallback_prompt(product, use_case, clip_type, script_segment)
        except Exception as e:
            self._logger.error(f"GPT-4o prompt generation failed: {e}")
            return self._fallback_prompt(product, use_case, clip_type, script_segment)
    
    def _build_system_prompt(self) -> str:
        """Build the system prompt for GPT-4o."""
        return """You are an expert AI video director specializing in short-form product videos for TikTok, Reels, and Shorts.

Your task: Create a detailed video generation prompt for a single clip in a multi-clip product video.

CRITICAL - PRODUCT INTEGRATION:
The product image provided is the STARTING FRAME for image-to-video generation. Your prompt MUST explicitly describe the product FROM THE IMAGE being integrated INTO the scene, not as a separate element.

ROLE IN STORYTELLING:
- HOOK: First 3 seconds. Must stop the scroll. High energy, pattern interrupt, visual curiosity.
- PROBLEM: Show the pain point or "before" state. Relatable struggle, emotional connection.
- SOLUTION: The product as hero. Transformation, benefits, the "aha" moment.
- BENEFITS: Lifestyle payoff. Happiness, satisfaction, aspiration.
- SOCIAL_PROOF: Trust signals. Others enjoying, community, validation.
- CTA: Final 3 seconds. Clear product focus, urgency, memorable closing image.

OUTPUT FORMAT (JSON):
{
    "visual_prompt": "Detailed description of what to generate (15-30 words). MUST start with 'The product from the first frame shown...' or similar integration phrase. Include: subject, action, setting, lighting, camera movement, style.",
    "motion_direction": "Specific camera and subject motion (e.g., 'slow push-in on product', 'hand enters frame from right', 'smooth orbit around subject')",
    "mood": "Emotional tone and visual atmosphere (e.g., 'energetic and aspirational', 'calm and luxurious', 'playful and vibrant')"
}

RULES:
- CRITICAL: The visual_prompt MUST explicitly reference the product from the image (e.g., "The product shown in the first frame is displayed...", "Featuring the product from the opening image...")
- The product should feel naturally INTEGRATED into the scene, not pasted on top
- Match the visual style to the product image provided
- Align with the voiceover script segment - visuals and words should tell the same story
- Consider the target audience - what visuals will resonate with them?
- Keep prompts concise but vivid - avoid generic phrases like "high quality" or "professional"
- Focus on SPECIFIC visual actions and compositions
- The product image is the starting frame - the video should animate FROM this image naturally"""

    def _build_user_content(
        self,
        product: Any,
        use_case: Any,
        clip_type: str,
        clip_index: int,
        total_clips: int,
        script_segment: str,
        full_script: str,
        image_path: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Build the user content for GPT-4o, including image if available."""
        
        # Build text description
        product_name = getattr(product, 'name', 'Product')
        product_desc = getattr(product, 'description', '')
        product_specs = getattr(product, 'specifications', {}) or {}
        
        style = getattr(use_case, 'style', 'realistic')
        video_format = getattr(use_case, 'format', '9:16')
        goal = getattr(use_case, 'goal', 'Learn more')
        target_audience = getattr(use_case, 'target_audience', 'consumers')
        
        style_descriptors = {
            'realistic': 'photorealistic, natural lighting, authentic',
            'cinematic': 'cinematic, dramatic lighting, film quality, anamorphic',
            'animated': '3D animation, vibrant colors, smooth motion, playful',
            'comic': 'comic book style, bold outlines, dynamic composition'
        }
        
        format_descriptors = {
            '9:16': 'vertical/portrait format, mobile-optimized',
            '16:9': 'horizontal/landscape format, cinematic widescreen',
            '1:1': 'square format, social media optimized',
            '4:5': 'vertical format, Instagram-friendly'
        }
        
        # Add scene context if provided
        scene_context_text = ""
        scene_instruction = ""
        scene_context_text = ""
        if hasattr(self, '_scene_context') and self._scene_context:
            scene_context_text = f"\n=== SCENE CONTEXT ===\nThe product should appear {self._scene_context}\n"
            scene_instruction = "5. Incorporates the scene context provided above"

        # Pre-format specs to avoid nested f-string issues
        specs_str = ', '.join(['{}: {}'.format(k, v) for k, v in list(product_specs.items())[:3]]) if product_specs else 'N/A'

        text_content = f"""=== CLIP INFORMATION ===
Clip Position: {clip_index + 1} of {total_clips}
Narrative Role: {clip_type.upper()}
Target Audience: {target_audience}
Video Style: {style_descriptors.get(style, style)}
Video Format: {format_descriptors.get(video_format, video_format)}

=== PRODUCT ===
Name: {product_name}
Description: {product_desc[:200] if product_desc else 'N/A'}
Key Specs: {specs_str}
{scene_context_text}
=== SCRIPT FOR THIS CLIP ===
"{script_segment}"

=== FULL VIDEO CONTEXT ===
Full Script: {full_script[:300]}...
Call to Action: {goal}

=== YOUR TASK ===
Create a video generation prompt for this {clip_type} clip.

The clip's narrative role is: {clip_type}
- Hook = attention-grabbing opener
- Problem = show the struggle/pain point
- Solution = product as hero, transformation
- Benefits = lifestyle payoff, happiness
- Social Proof = trust, others using it
- CTA = clear product focus, urgency

Analyze the product image and create a prompt that:
1. **CRITICAL**: The visual_prompt MUST explicitly integrate the product FROM THE IMAGE into the scene (e.g., "The product shown in the first frame is beautifully displayed on a kitchen counter...")
2. Uses the product's actual visual characteristics
3. Matches the voiceover script's message
4. Fits the {clip_type} role in the story arc
5. Appeals to {target_audience}
{scene_instruction}

**PRODUCT INTEGRATION EXAMPLES:**
- Good: "The product from the first frame sits elegantly on a marble countertop, soft morning light highlighting its sleek design..."
- Good: "Featuring the product shown in the opening image, now in use within a modern kitchen setting..."
- Bad: "A kitchen scene with modern appliances..." (doesn't mention the product from the image)

The product should feel NATURALLY PART OF the scene, not like an overlay. The video animates FROM the provided image.

Return ONLY the JSON object with visual_prompt, motion_direction, and mood."""

        # Build content array
        content = [{"type": "text", "text": text_content}]
        
        # Add image if available
        if image_path:
            image_b64 = self._encode_image(image_path)
            if image_b64:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_b64}",
                        "detail": "high"
                    }
                })
        
        return content
    
    def _fallback_prompt(
        self,
        product: Any,
        use_case: Any,
        clip_type: str,
        script_segment: str,
        generation_strategy: str = 'composite_then_kling'
    ) -> Dict[str, str]:
        """Fallback template-based prompt generation."""
        product_name = getattr(product, 'name', 'Product')
        style = getattr(use_case, 'style', 'realistic')
        video_format = getattr(use_case, 'format', '9:16')
        scene_context = getattr(self, '_scene_context', None)
        
        style_desc = {
            'realistic': 'photorealistic, natural lighting',
            'cinematic': 'cinematic, dramatic lighting',
            'animated': '3D animation, vibrant colors',
            'comic': 'comic book style, bold outlines'
        }.get(style, 'photorealistic')
        
        format_desc = {
            '9:16': 'vertical video format',
            '16:9': 'horizontal widescreen format',
            '1:1': 'square format',
            '4:5': 'vertical portrait format'
        }.get(video_format, 'vertical format')
        
        if generation_strategy == 'world_model_broll':
            visual_prompt = self._build_broll_prompt(clip_type, script_segment, style_desc, format_desc, scene_context)
            return {
                'visual_prompt': visual_prompt,
                'motion_direction': 'gentle parallax and cinematic camera drift',
                'camera_prompt': 'steady dolly or slider movement with smooth easing',
                'atmosphere_prompt': f'cinematic ambience, {style_desc}',
                'mood': 'cinematic and story-driven'
            }

        visual_prompt = self._build_motion_only_product_prompt(
            product_name=product_name,
            clip_type=clip_type,
            script_segment=script_segment,
            style_desc=style_desc,
            format_desc=format_desc,
            scene_context=scene_context,
            strategy=generation_strategy
        )

        return {
            'visual_prompt': visual_prompt,
            'motion_direction': 'slow push-in, subtle orbit, stabilized cinematic framing',
            'camera_prompt': 'macro-to-medium lens cadence, smooth gimbal movement',
            'atmosphere_prompt': f'soft commercial lighting, premium texture detail, {style_desc}',
            'mood': 'premium and product-focused'
        }

    def _build_motion_only_product_prompt(
        self,
        product_name: str,
        clip_type: str,
        script_segment: str,
        style_desc: str,
        format_desc: str,
        scene_context: Optional[str],
        strategy: str
    ) -> str:
        """Build motion/camera/atmosphere-focused prompts for product-led clips."""
        beat = {
            'hook': 'opening beat with confident energy',
            'solution': 'solution reveal beat',
            'cta': 'closing beat with purchase intent',
            'product_demo': 'hands-on usage beat',
            'product_showcase': 'hero showcase beat'
        }.get(clip_type, 'product storytelling beat')
        context_hint = f" Script emphasis: {script_segment}" if script_segment else ''
        scene_hint = f" Scene anchor context: {scene_context}." if scene_context else ''
        strategy_hint = 'Use the provided composite frame as the scene anchor.' if strategy == 'composite_then_kling' else 'Use the provided product image as the anchor.'
        return (
            f"{strategy_hint} Keep product geometry and branding stable for {product_name}. "
            f"Focus only on motion, camera path, and atmosphere for a {beat}. "
            f"Add subtle depth motion and premium lighting shifts. {style_desc}, {format_desc}.{scene_hint}{context_hint}"
        )

    def _build_broll_prompt(
        self,
        clip_type: str,
        script_segment: str,
        style_desc: str,
        format_desc: str,
        scene_context: Optional[str]
    ) -> str:
        """Build text-first narrative prompts for world model b-roll clips."""
        beat = clip_type.replace('_', ' ')
        script_hint = f"Narrative cue: {script_segment}." if script_segment else ''
        scene_hint = f"Scene context: {scene_context}." if scene_context else ''
        return (
            f"Cinematic {beat} b-roll scene with expressive environmental action and human context. "
            f"No explicit product close-up required. Prioritize atmosphere, storytelling transitions, and emotional clarity. "
            f"{style_desc}, {format_desc}. {scene_hint} {script_hint}".strip()
        )


# Convenience function for direct usage
def generate_clip_prompts(
    product: Any,
    use_case: Any,
    script_content: str,
    product_images: List[str],
    api_key: Optional[str] = None,
    num_clips: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Generate clip prompts using GPT-4o Vision.
    
    Convenience function that creates a ClipPromptGenerator instance.
    """
    generator = ClipPromptGenerator(api_key=api_key)
    return generator.generate_clip_prompts(
        product=product,
        use_case=use_case,
        script_content=script_content,
        product_images=product_images,
        num_clips=num_clips
    )
