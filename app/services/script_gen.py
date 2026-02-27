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
                base_url="https://api.moonshot.cn/v1",
                http_client=http_client
            )
        elif not offline_fallback:
            raise ValueError("MOONSHOT_API_KEY is required when offline fallback is disabled")

    # ------------------------------------------------------------------
    @api_retry(label="kimi_chat", exceptions=(Exception,))
    def _chat_completion(self, messages, **kwargs):
        if not self.client:
            raise RuntimeError("Kimi client is not configured")
        return self.client.chat.completions.create(messages=messages, **kwargs)

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
                model="kimi-k2-5",
                temperature=0.8,
                max_tokens=500,
            )
            script_content = response.choices[0].message.content.strip()
            script_content = self._clean_script(script_content)
            word_count = len(script_content.split())
            estimated_duration = self._estimate_duration(script_content)
            return {
                "success": True,
                "content": script_content,
                "estimated_duration": estimated_duration,
                "word_count": word_count,
                "tone": use_case_config.get("style", "conversational"),
                "generation_prompt": user_prompt,
                "raw_response": response.choices[0].message.content,
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
        system_prompt = (
            "You are an expert copywriter. Refine the provided script while keeping it "
            f"optimized for a {duration}-second video in a {style} tone."
        )
        user_prompt = f"""Current Script:
{current_script}

Refinement Request:
{refinement_request}

Provide ONLY the refined script text. No explanations."""

        try:
            response = self._chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="kimi-k2-5",
                temperature=0.8,
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
        tone_guidance = {
            "realistic": "conversational, authentic, and relatable",
            "cinematic": "dramatic, epic, and emotionally compelling",
            "animated": "energetic, playful, and engaging",
            "comic": "witty, punchy, and memorable",
        }
        tone = tone_guidance.get(style, "conversational and engaging")
        return f"""You are an expert copywriter specializing in short-form video scripts.
Create a {duration}-second script with a {tone} tone. Hook viewers quickly, highlight benefits, and end with a CTA.
Keep sentences short, natural, and easy to speak."""

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

        parts = [
            f"Create a script for {product_name}.",
            "",
            "Product Information:",
            f"- Name: {product_name}",
        ]
        if brand:
            parts.append(f"- Brand: {brand}")
        if price:
            parts.append(f"- Price: {price}")
        if description:
            parts.append(f"- Description: {description}")
        if key_specs:
            parts.extend(["", "Key Specifications:"] + key_specs)
        if review_lines:
            parts.extend(["", "Customer Reviews:"] + review_lines)
        parts.extend(
            [
                "",
                f"Target Audience: {target_audience}",
                f"Call to Action: {goal}",
                f"Target Duration: {duration} seconds",
                "",
                "Write ONLY the spoken script. No stage directions or markdown.",
            ]
        )
        if existing_script:
            parts.extend(["", "Previous script for context:", existing_script])
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
