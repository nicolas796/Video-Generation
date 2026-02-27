"""Script generation service using Kimi (Moonshot) API with offline fallback."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

import httpx
from openai import OpenAI

from app.utils import api_retry


class ScriptGenerator:
    """Create, refine, and estimate short-form video scripts."""

    def __init__(self, api_key: Optional[str] = None, *, offline_fallback: bool = True) -> None:
        self.api_key = api_key or os.getenv("MOONSHOT_API_KEY")
        self.offline_fallback = offline_fallback
        self.client: Optional[OpenAI] = None
        if self.api_key:
            # Use Kimi (Moonshot) API with OpenAI-compatible SDK
            # Create httpx client without proxy to avoid environment proxy issues
            http_client = httpx.Client(timeout=60.0, follow_redirects=True)
            self.client = OpenAI(
                api_key=self.api_key,
                base_url="https://api.moonshot.ai/v1",
                http_client=http_client
            )
        elif not offline_fallback:
            raise ValueError("MOONSHOT_API_KEY is required when offline fallback is disabled")

    # ------------------------------------------------------------------
    @api_retry(label="kimi_chat", exceptions=(Exception,))
    def _chat_completion(self, messages, **kwargs):
        if not self.client:
            raise RuntimeError("Kimi client is not configured")
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Calling Moonshot API with model: {kwargs.get('model')}")
        try:
            return self.client.chat.completions.create(messages=messages, **kwargs)
        except Exception as e:
            logger.error(f"Moonshot API error: {type(e).__name__}: {e}")
            raise

    # ------------------------------------------------------------------
    def generate_script(
        self,
        product_data: Dict[str, Any],
        use_case_config: Dict[str, Any],
        existing_script: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate a fresh script or reuse offline fallback when API fails."""

        if not self.client:
            return self._offline_script(product_data, use_case_config)

        system_prompt = self._build_system_prompt(use_case_config)
        user_prompt = self._build_user_prompt(product_data, use_case_config, existing_script)

        try:
            response = self._chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="kimi-k2.5",
                temperature=1.0,
                max_tokens=500,
            )
            
            # Safely extract content from response
            if not response or not response.choices:
                raise RuntimeError("API returned empty response or no choices")
            
            message = response.choices[0].message
            if not message or not message.content:
                raise RuntimeError("API returned empty message content")
            
            raw_content = message.content
            script_content = self._clean_script(raw_content.strip())
            
            if not script_content:
                return {"success": False, "error": "Script generation returned empty content after cleaning", "content": None, "estimated_duration": None}
            
            word_count = len(script_content.split())
            estimated_duration = self._estimate_duration(script_content)
            return {
                "success": True,
                "content": script_content,
                "estimated_duration": estimated_duration,
                "word_count": word_count,
                "tone": use_case_config.get("style", "conversational"),
                "generation_prompt": user_prompt,
                "raw_response": raw_content,
            }
        except Exception as exc:
            if not self.offline_fallback:
                return {"success": False, "error": str(exc), "content": None, "estimated_duration": None}
            fallback = self._offline_script(product_data, use_case_config)
            fallback["warning"] = f"Kimi API failed: {exc}"
            return fallback

    def refine_script(
        self,
        current_script: str,
        refinement_request: str,
        product_data: Dict[str, Any],
        use_case_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Refine an existing script or fall back to deterministic edits."""

        if not self.client:
            return self._offline_refine(current_script, refinement_request, use_case_config)

        style = use_case_config.get("style", "realistic")
        duration = use_case_config.get("duration_target", 15)
        word_target = int(duration * 2.5)
        
        style_guidance = {
            "realistic": "conversational, authentic, and relatable",
            "cinematic": "dramatic, epic, and emotionally compelling",
            "animated": "energetic, playful, and engaging",
            "comic": "witty, punchy, and memorable",
        }
        tone = style_guidance.get(style, "conversational and engaging")
        
        system_prompt = (
            "You are an expert copywriter for short-form video ads. "
            f"Refine scripts to be {tone} and exactly {word_target} words for a {duration}-second video. "
            "Maintain: Hook → Problem → Solution → CTA structure. "
            "Keep it punchy, benefit-focused, and scroll-stopping."
        )
        user_prompt = f"""CURRENT SCRIPT ({word_target} words target):
{current_script}

REFINEMENT REQUEST:
{refinement_request}

RULES:
- Keep the {word_target} word count (critical!)
- Maintain Hook → Problem → Solution → CTA flow
- Short sentences, one breath each
- Focus on feelings/outcomes, not features

OUTPUT: Only the refined script text."""

        try:
            response = self._chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="kimi-k2.5",
                temperature=1.0,
                max_tokens=500,
            )
            script_content = self._clean_script(response.choices[0].message.content.strip())
            return {
                "success": True,
                "content": script_content,
                "estimated_duration": self._estimate_duration(script_content),
                "word_count": len(script_content.split()),
                "tone": style,
            }
        except Exception as exc:
            if not self.offline_fallback:
                return {"success": False, "error": str(exc), "content": current_script}
            return self._offline_refine(current_script, refinement_request, use_case_config, error=str(exc))

    # ------------------------------------------------------------------
    def _offline_script(self, product_data: Dict[str, Any], use_case_config: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic script used when Kimi API is unavailable."""

        name = product_data.get("name") or "this product"
        benefit = (product_data.get("specifications") or {}).get("Key benefit")
        goal = use_case_config.get("goal") or "Tap to learn more"
        audience = use_case_config.get("target_audience") or "people like you"
        duration = use_case_config.get("duration_target", 15)

        lines = [
            f"Hey {audience}! Meet {name}.",
            benefit or "Imagine getting pro-level results without the pro-level effort.",
            product_data.get("description", "It's built to make everyday moments brighter."),
            goal,
        ]
        script = " ".join(line.strip() for line in lines if line)
        script = self._clean_script(script)
        estimated_duration = max(1, min(45, duration))
        return {
            "success": True,
            "content": script,
            "estimated_duration": estimated_duration,
            "word_count": len(script.split()),
            "tone": use_case_config.get("style", "conversational"),
            "generation_prompt": "offline_template",
        }

    def _offline_refine(
        self,
        current_script: str,
        refinement_request: str,
        use_case_config: Dict[str, Any],
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        addition = refinement_request.strip().rstrip('.')
        if addition:
            refined = f"{current_script.strip()} {addition}."
        else:
            refined = current_script
        return {
            "success": True,
            "content": refined,
            "estimated_duration": self._estimate_duration(refined),
            "word_count": len(refined.split()),
            "tone": use_case_config.get("style", "conversational"),
            "warning": error,
        }

    # ------------------------------------------------------------------
    def _build_system_prompt(self, use_case_config: Dict[str, Any]) -> str:
        style = use_case_config.get("style", "realistic")
        duration = use_case_config.get("duration_target", 15)
        # Word count target: ~2.5 words per second for natural speech
        word_target = int(duration * 2.5)
        
        style_guidance = {
            "realistic": {
                "tone": "conversational, authentic, and relatable - like a friend recommending something",
                "approach": "Personal, direct-to-camera style. Use 'you' and 'I'. Share a quick personal insight."
            },
            "cinematic": {
                "tone": "dramatic, epic, and emotionally compelling",
                "approach": "High stakes, aspirational language. Create a mini-story with transformation."
            },
            "animated": {
                "tone": "energetic, playful, and engaging",
                "approach": "Fun, bouncy, exclamation-friendly. Use rhythm and repetition."
            },
            "comic": {
                "tone": "witty, punchy, and memorable",
                "approach": "Clever wordplay, unexpected twist, or relatable humor."
            },
        }
        style_info = style_guidance.get(style, style_guidance["realistic"])
        
        return f"""You are an expert copywriter for viral short-form video ads (TikTok, Reels, Shorts).

MISSION: Create a {duration}-second script ({word_target} words) that stops the scroll and drives action.

STRUCTURE (follow this exactly):
1. HOOK (first 3 seconds): Pattern interrupt. Ask a question, state a bold claim, or call out the viewer directly.
2. PROBLEM/CONTEXT (3-7 seconds): The pain point or desire this product solves.
3. SOLUTION (7-{duration-3} seconds): The product as the hero. ONE key benefit (not feature). Make it tangible.
4. CTA (last 3 seconds): Clear, urgent call-to-action.

STYLE: {style_info['tone']}
APPROACH: {style_info['approach']}

RULES:
- EXACTLY {word_target} words (not more, not less)
- Benefits > Features (what it DOES for them, not what it IS)
- One breath per sentence - short and punchy
- No lists of specs or attributes
- No "Introducing..." or "Meet the..." - start with the hook
- End with the exact CTA phrase provided
- Respond in English only

EXAMPLES BY STYLE:
Realistic: "Okay, I was skeptical too. But this thing actually cut my morning routine in half. No more frizz, no more heat damage. Just smooth hair in 5 minutes. The Dyson Airwrap? Game changer. Link in bio before they sell out again."

Cinematic: "Every sunrise, a choice. Stay the same, or become who you were meant to be. This isn't just a watch. It's 365 days of discipline, wrapped around your wrist. The moment is now. Tag someone who's ready."

Animated: "POV: You just discovered the snack that hits different! Crunchy, spicy, sweet - these Korean chips have NO business being this good! I ate the whole bag in one sitting. Stock up before I buy them all! Link below!"

Comic: "Me before coffee: [insert zombie sounds]. Me after this mug: actually functional human. This isn't coffee, it's liquid personality restoration. Get yours before I finish the supply. Link in comments."""

    def _build_user_prompt(
        self,
        product_data: Dict[str, Any],
        use_case_config: Dict[str, Any],
        existing_script: Optional[str] = None,
    ) -> str:
        product_name = product_data.get("name", "Product")
        description = product_data.get("description", "")
        brand = product_data.get("brand", "")
        price = product_data.get("price", "")
        specs = product_data.get("specifications", {})
        reviews = product_data.get("reviews", [])
        goal = use_case_config.get("goal", "Learn more today")
        target_audience = use_case_config.get("target_audience", "modern shoppers")
        duration = use_case_config.get("duration_target", 15)

        key_specs = []
        if isinstance(specs, dict):
            for key, value in list(specs.items())[:5]:
                key_specs.append(f"- {key}: {value}")

        review_lines = []
        if isinstance(reviews, list):
            for review in reviews[:2]:
                if isinstance(review, dict):
                    text = review.get("text") or review.get("review") or ""
                    if text:
                        review_lines.append(f'- "{text[:100]}..."')

        # Extract ONE key benefit from description or specs
        key_benefit = description[:100] if description else ""
        if not key_benefit and key_specs:
            key_benefit = key_specs[0]
        
        # Get social proof snippet
        social_proof = ""
        if review_lines:
            social_proof = review_lines[0].strip('"-')
        
        word_target = int(duration * 2.5)
        
        parts = [
            f"TASK: Write a {duration}-second viral video script for {product_name}.",
            f"WORD COUNT TARGET: Exactly {word_target} words (THIS IS CRITICAL - count your words!)",
            "",
            "=== PRODUCT DETAILS ===",
            f"Product: {product_name}",
        ]
        if brand:
            parts.append(f"Brand: {brand}")
        parts.append(f"Target Audience: {target_audience}")
        parts.append(f"The ONE Thing They Care About: {key_benefit}")
        if social_proof:
            parts.append(f"Social Proof: A customer said '{social_proof}'")
        parts.extend([
            "",
            "=== YOUR MISSION ===",
            f"1. HOOK (3 sec): Grab {target_audience} immediately - what pain point or desire stops their scroll?",
            f"2. PROBLEM (2-4 sec): The 'before' state - what sucks without this product?",
            f"3. SOLUTION (5-{duration-5} sec): The transformation - what's different now? Use EMOTION, not specs.",
            f"4. CTA (2 sec): '{goal}' - make it feel urgent",
            "",
            "=== REMINDERS ===",
            f"- EXACTLY {word_target} words (use wordcounter.net if needed)",
            "- Start with the HOOK, not 'Introducing' or product name",
            "- Focus on FEELINGS and OUTCOMES, not features",
            "- One short sentence per line for readability",
            "- End with the exact CTA phrase",
            "",
            "OUTPUT: Just the spoken script. No quotes, no formatting.",
        ])
        if existing_script:
            parts.extend(["", "=== PREVIOUS ATTEMPT (avoid this approach) ===", existing_script])
        return "\n".join(parts)

    @staticmethod
    def _clean_script(script: str) -> str:
        script = script.strip()
        if script.startswith(("'", '"')) and script.endswith(("'", '"')):
            script = script[1:-1]
        script = re.sub(r'^```\w*\n?', '', script)
        script = re.sub(r'\n?```$', '', script)
        script = re.sub(r'\[.*?\]', '', script)
        script = re.sub(r'\(.*?\)', '', script)
        script = ' '.join(script.split())
        return script.strip()

    @staticmethod
    def _estimate_duration(script: str) -> int:
        word_count = len(script.split())
        return max(1, int(word_count / 2.3))


def generate_script(
    product_data: Dict[str, Any],
    use_case_config: Dict[str, Any],
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    return ScriptGenerator(api_key=api_key).generate_script(product_data, use_case_config)
