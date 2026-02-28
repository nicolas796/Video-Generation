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
            http_client = httpx.Client(timeout=60.0, follow_redirects=True)
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
        scene_context: Optional[str] = None
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
        
        for i, clip_type in enumerate(clip_types):
            # Select the best product image for this clip type
            image_path = self._select_image_for_clip(product_images, clip_type, i)
            
            # Generate AI-powered prompt
            prompt_data = self._generate_ai_prompt(
                product=product,
                use_case=use_case,
                clip_type=clip_type,
                clip_index=i,
                total_clips=num_clips,
                script_segment=script_segments[i] if i < len(script_segments) else "",
                full_script=script_content,
                image_path=image_path
            )
            
            clips_config.append({
                'sequence_order': i,
                'clip_type': clip_type,
                'prompt': prompt_data.get('visual_prompt', ''),
                'motion_direction': prompt_data.get('motion_direction', ''),
                'mood': prompt_data.get('mood', ''),
                'estimated_duration': 5,
                'image_path': image_path
            })
        
        return clips_config
    
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
            
            # Make the API call
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.8,
                max_tokens=800,
                response_format={"type": "json_object"}
            )
            
            # Parse the response
            content = response.choices[0].message.content
            result = json.loads(content)
            
            return {
                'visual_prompt': result.get('visual_prompt', ''),
                'motion_direction': result.get('motion_direction', ''),
                'mood': result.get('mood', '')
            }
            
        except Exception as e:
            self._logger.error(f"GPT-4o prompt generation failed: {e}")
            return self._fallback_prompt(product, use_case, clip_type, script_segment)
    
    def _build_system_prompt(self) -> str:
        """Build the system prompt for GPT-4o."""
        return """You are an expert AI video director specializing in short-form product videos for TikTok, Reels, and Shorts.

Your task: Create a detailed video generation prompt for a single clip in a multi-clip product video.

ROLE IN STORYTELLING:
- HOOK: First 3 seconds. Must stop the scroll. High energy, pattern interrupt, visual curiosity.
- PROBLEM: Show the pain point or "before" state. Relatable struggle, emotional connection.
- SOLUTION: The product as hero. Transformation, benefits, the "aha" moment.
- BENEFITS: Lifestyle payoff. Happiness, satisfaction, aspiration.
- SOCIAL_PROOF: Trust signals. Others enjoying, community, validation.
- CTA: Final 3 seconds. Clear product focus, urgency, memorable closing image.

OUTPUT FORMAT (JSON):
{
    "visual_prompt": "Detailed description of what to generate (15-30 words). Include: subject, action, setting, lighting, camera movement, style.",
    "motion_direction": "Specific camera and subject motion (e.g., 'slow push-in on product', 'hand enters frame from right', 'smooth orbit around subject')",
    "mood": "Emotional tone and visual atmosphere (e.g., 'energetic and aspirational', 'calm and luxurious', 'playful and vibrant')"
}

RULES:
- Match the visual style to the product image provided
- Align with the voiceover script segment - visuals and words should tell the same story
- Consider the target audience - what visuals will resonate with them?
- Keep prompts concise but vivid - avoid generic phrases like "high quality" or "professional"
- Focus on SPECIFIC visual actions and compositions
- The prompt will be used with image-to-video AI (the product image is the starting frame)"""

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
        if hasattr(self, '_scene_context') and self._scene_context:
            scene_context_text = f"\n=== SCENE CONTEXT ===\nThe product should appear {self._scene_context}\n"

        text_content = f"""=== CLIP INFORMATION ===
Clip Position: {clip_index + 1} of {total_clips}
Narrative Role: {clip_type.upper()}
Target Audience: {target_audience}
Video Style: {style_descriptors.get(style, style)}
Video Format: {format_descriptors.get(video_format, video_format)}

=== PRODUCT ===
Name: {product_name}
Description: {product_desc[:200] if product_desc else 'N/A'}
Key Specs: {', '.join([f"{k}: {v}" for k, v in list(product_specs.items())[:3]]) if product_specs else 'N/A'}
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
1. Uses the product's actual visual characteristics
2. Matches the voiceover script's message
3. Fits the {clip_type} role in the story arc
4. Appeals to {target_audience}{"\n5. Incorporates the scene context provided above" if scene_context_text else ""}

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
        script_segment: str
    ) -> Dict[str, str]:
        """Fallback template-based prompt generation."""
        product_name = getattr(product, 'name', 'Product')
        style = getattr(use_case, 'style', 'realistic')
        video_format = getattr(use_case, 'format', '9:16')
        
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
        
        clip_prompts = {
            'hook': f"Eye-catching opening shot of {product_name}. Dynamic camera movement, engaging visual. {style_desc}, {format_desc}",
            'problem': f"Scene showing the problem that {product_name} solves. Relatable situation, emotional connection. {style_desc}",
            'solution': f"Beautiful demonstration of {product_name} solving the problem. Transformation moment. {style_desc}",
            'benefits': f"Lifestyle scene showing satisfaction from using {product_name}. Happy person, aspirational setting. {style_desc}",
            'social_proof': f"Scene suggesting people enjoying {product_name}. Positive atmosphere, community feeling. {style_desc}",
            'cta': f"Strong closing scene with {product_name} front and center. Clear view, memorable final image. {style_desc}",
            'product_showcase': f"Stunning highlight of {product_name}. Multiple angles, premium quality. {style_desc}",
            'product_demo': f"Step by step demonstration of {product_name} in action. Clear visibility. {style_desc}"
        }
        
        return {
            'visual_prompt': clip_prompts.get(clip_type, clip_prompts['product_showcase']),
            'motion_direction': 'smooth camera movement, professional composition',
            'mood': 'engaging and professional'
        }


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
