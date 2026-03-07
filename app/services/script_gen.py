"""Script generation service using Kimi (Moonshot) API with offline fallback."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import httpx
from openai import OpenAI

from app.utils import api_retry

HOOK_STYLE_RULES: Dict[str, Dict[str, str]] = {
    "problem-agitation": {
        "tone": "urgent",
        "problem_suffix": "Every wasted second bleeds attention and ad spend.",
        "solution_template": "Here's the fix: {product} {benefit}.",
        "cta_prefix": "Fix it now:",
    },
    "bold-claim": {
        "tone": "confident",
        "problem_suffix": "Big promises flop without proof.",
        "solution_template": "We back it up: {product} {benefit} so the numbers stick.",
        "cta_prefix": "Prove it yourself:",
    },
    "status-quo-flip": {
        "tone": "contrarian",
        "problem_suffix": "Playing it safe keeps you invisible.",
        "solution_template": "Flip it—{product} {benefit} so you stand out on frame one.",
        "cta_prefix": "Flip the script:",
    },
    "specific-outcome": {
        "tone": "precise",
        "problem_suffix": "Tiny tweaks decide who wins the feed.",
        "solution_template": "Dial it in with {product}: {benefit}.",
        "cta_prefix": "Lock it in:",
    },
    "enemy-of-waste": {
        "tone": "protective",
        "problem_suffix": "Every lag is money leaking out.",
        "solution_template": "Take it back—{product} {benefit} before viewers swipe.",
        "cta_prefix": "Stop the leak:",
    },
    "direct-question": {
        "tone": "conversational",
        "problem_suffix": "If you hesitated, it's already costing you.",
        "solution_template": "Answer it with {product}: {benefit}.",
        "cta_prefix": "Get the answer:",
    },
    "provocative": {
        "tone": "bold",
        "problem_suffix": "Admitting it first lets you own the upside.",
        "solution_template": "Here's the truth—{product} {benefit} while everyone else stalls.",
        "cta_prefix": "Own the win:",
    },
    "value-prop": {
        "tone": "assured",
        "problem_suffix": "Forget fluff—outcomes are all that matter.",
        "solution_template": "{product} simply {benefit}.",
        "cta_prefix": "Claim it:",
    },
    "shocking-stat": {
        "tone": "authoritative",
        "problem_suffix": "The data doesn't lie about drop-off.",
        "solution_template": "{product} {benefit} so you're on the right side of the stat.",
        "cta_prefix": "Beat the metric:",
    },
}

DEFAULT_HOOK_RULE = {
    "tone": "conversational",
    "problem_suffix": "Viewers feel the drag immediately.",
    "solution_template": "{product} {benefit} so you stay top-of-feed.",
    "cta_prefix": "Take a look:",
}

PAIN_SPEC_KEYS: List[str] = [
    "Pain",
    "Pain point",
    "Problem",
    "Challenge",
    "Frustration",
    "Issue",
    "Obstacle",
]

BENEFIT_SPEC_KEYS: List[str] = [
    "Key benefit",
    "Benefit",
    "Outcome",
    "Result",
    "Advantage",
    "Value",
]


class ScriptGenerator:
    """Create, refine, and estimate short-form video scripts."""

    def __init__(self, api_key: Optional[str] = None, *, offline_fallback: bool = True) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.offline_fallback = offline_fallback
        self.client: Optional[OpenAI] = None
        if self.api_key:
            # Use OpenAI API for script generation
            # Create httpx client without proxy to avoid environment proxy issues
            http_client = httpx.Client(timeout=60.0, follow_redirects=True)
            self.client = OpenAI(
                api_key=self.api_key,
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

    def _expand_hook_to_script(
        self,
        hook: Dict[str, Any],
        product_data: Dict[str, Any],
        use_case_config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        variant = dict((hook or {}).get("variant") or {})
        hook_text = variant.get("verbal") or variant.get("on_screen") or variant.get("visual")
        hook_line = self._ensure_sentence(hook_text)
        if not hook_line:
            return None

        hook_type = (hook.get("hook_type") or "").lower()
        rules = HOOK_STYLE_RULES.get(hook_type, DEFAULT_HOOK_RULE)
        audience = self._resolve_audience(product_data, use_case_config)
        problem_hint = self._extract_primary_problem(product_data, hook_line)
        problem_line = self._compose_problem_sentence(problem_hint, audience, rules)
        benefit_phrase = self._extract_primary_benefit(product_data)
        benefit_clause = self._format_benefit_clause(benefit_phrase)
        solution_line = self._compose_solution_sentence(rules, product_data.get("name"), benefit_clause, variant)
        cta_line = self._format_cta_text(use_case_config.get("goal") or "Tap to learn more", rules.get("cta_prefix"))

        sections = {
            "hook": hook_line,
            "problem": problem_line,
            "solution": solution_line,
            "cta": cta_line,
        }
        script = " ".join(section for section in sections.values() if section).strip()
        if not script:
            return None

        return {
            "success": True,
            "content": script,
            "estimated_duration": self._estimate_duration(script),
            "word_count": len(script.split()),
            "tone": rules.get("tone", use_case_config.get("style", "conversational")),
            "generation_prompt": "hook_blueprint",
            "sections": sections,
            "hook_context": {
                "hook_id": hook.get("id"),
                "hook_type": hook_type or None,
                "variant_index": hook.get("variant_index"),
            },
        }

    # ------------------------------------------------------------------
    def generate_script(
        self,
        product_data: Dict[str, Any],
        use_case_config: Dict[str, Any],
        existing_script: Optional[str] = None,
        *,
        hook: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate a fresh script or reuse offline fallback when API fails."""

        hook_outline = None
        if hook:
            hook_outline = self._expand_hook_to_script(hook, product_data, use_case_config)

        if not self.client:
            return hook_outline or self._offline_script(product_data, use_case_config)

        system_prompt = self._build_system_prompt(use_case_config, hook_hint=hook_outline)
        user_prompt = self._build_user_prompt(
            product_data,
            use_case_config,
            existing_script,
            hook=hook,
            hook_hint=hook_outline,
        )

        try:
            response = self._chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="gpt-4o",
                temperature=0.8,
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
            script_content = self._enforce_hook_line(script_content, hook_outline)

            if not script_content:
                return {"success": False, "error": "Script generation returned empty content after cleaning", "content": None, "estimated_duration": None}

            word_count = len(script_content.split())
            estimated_duration = self._estimate_duration(script_content)
            result_payload = {
                "success": True,
                "content": script_content,
                "estimated_duration": estimated_duration,
                "word_count": word_count,
                "tone": (hook_outline or {}).get("tone", use_case_config.get("style", "conversational")),
                "generation_prompt": user_prompt,
                "raw_response": raw_content,
            }
            if hook_outline:
                result_payload["hook_context"] = hook_outline.get("hook_context")
                result_payload["sections"] = hook_outline.get("sections")
            return result_payload
        except Exception as exc:
            if not self.offline_fallback:
                return {"success": False, "error": str(exc), "content": None, "estimated_duration": None}
            if hook_outline:
                fallback = dict(hook_outline)
                fallback["warning"] = f"Kimi API failed: {exc}"
                return fallback
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
                model="gpt-4o",
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

    def _resolve_audience(self, product_data: Dict[str, Any], use_case_config: Dict[str, Any]) -> str:
        audience = use_case_config.get("target_audience")
        specs = product_data.get("specifications") or {}
        if not audience and isinstance(specs, dict):
            audience = specs.get("Audience") or specs.get("audience")
        if not audience:
            audience = product_data.get("target_audience")
        return (audience or "creators").strip()

    def _extract_primary_problem(self, product_data: Dict[str, Any], hook_line: str) -> str:
        specs = product_data.get("specifications") or {}
        normalized = {}
        if isinstance(specs, dict):
            normalized = {str(key).lower(): value for key, value in specs.items()}
        for key in PAIN_SPEC_KEYS:
            value = normalized.get(key.lower())
            if isinstance(value, str) and value.strip():
                return value.strip()
        description = product_data.get("description") or ""
        if description:
            first_sentence = re.split(r"[.!?]", description)[0].strip()
            if first_sentence:
                return first_sentence
        return self._normalize_issue_phrase(hook_line)

    def _extract_primary_benefit(self, product_data: Dict[str, Any]) -> str:
        specs = product_data.get("specifications") or {}
        normalized = {}
        if isinstance(specs, dict):
            normalized = {str(key).lower(): value for key, value in specs.items()}
        for key in BENEFIT_SPEC_KEYS:
            value = normalized.get(key.lower())
            if isinstance(value, str) and value.strip():
                return value.strip()
        description = product_data.get("description") or ""
        if description:
            sentences = [s.strip() for s in re.split(r"[.!?]", description) if s.strip()]
            if sentences:
                return sentences[0]
        reviews = product_data.get("reviews") or []
        if isinstance(reviews, list):
            for review in reviews:
                if isinstance(review, dict):
                    text = review.get("text") or review.get("review")
                    if text:
                        return text.strip()
                elif isinstance(review, str) and review.strip():
                    return review.strip()
        return "makes results feel effortless"

    def _format_benefit_clause(self, benefit: str) -> str:
        clause = (benefit or "makes results feel effortless").strip().rstrip('.')
        if not clause:
            clause = "makes results feel effortless"
        if clause and clause[0].isupper():
            clause = clause[0].lower() + clause[1:]
        verbs = ("delivers", "gives", "adds", "saves", "keeps", "lets", "makes", "builds", "drives", "proves", "shows")
        if not clause.lower().startswith(verbs):
            clause = f"delivers {clause}"
        return clause

    def _compose_problem_sentence(self, problem_hint: str, audience: str, rules: Dict[str, Any]) -> str:
        clause = self._format_problem_clause(problem_hint)
        prefix_template = rules.get("problem_prefix") or "{audience} are tired of"
        prefix = prefix_template.replace("{audience}", audience)
        sentence = f"{prefix} {clause}."
        suffix = (rules.get("problem_suffix") or "").strip()
        if suffix:
            if suffix[-1] not in ".!?":
                suffix += "."
            sentence = f"{sentence} {suffix}"
        return sentence.strip()

    def _format_problem_clause(self, clause: str) -> str:
        value = (clause or "doing everything manually").strip().rstrip('.!?')
        if not value:
            value = "doing everything manually"
        return value

    def _compose_solution_sentence(
        self,
        rules: Dict[str, Any],
        product_name: Optional[str],
        benefit_clause: str,
        variant: Dict[str, Any],
    ) -> str:
        template = rules.get("solution_template") or DEFAULT_HOOK_RULE["solution_template"]
        product_label = product_name or "This product"
        sentence = template.format(product=product_label, benefit=benefit_clause)
        visual = (variant.get("visual") or "").strip()
        if visual:
            visual_body = visual.rstrip('.!?') or visual
            visual_sentence = f"Picture {visual_body}."
            sentence = f"{sentence} {visual_sentence}"
        credibility = (variant.get("credibility") or "").strip()
        if credibility:
            if credibility[-1] not in ".!?":
                credibility += "."
            sentence = f"{sentence} {credibility}"
        return sentence.strip()

    def _format_cta_text(self, goal: str, prefix: Optional[str]) -> str:
        goal_clean = (goal or "Tap to learn more").strip().rstrip('.')
        if not goal_clean:
            goal_clean = "Tap to learn more"
        prefix_clean = (prefix or "").strip()
        phrase = f"{prefix_clean} {goal_clean}".strip() if prefix_clean else goal_clean
        if phrase and phrase[-1] not in ".!?":
            phrase = f"{phrase}."
        return phrase

    def _ensure_sentence(self, value: Optional[str]) -> str:
        text = self._clean_script(value or "")
        if not text:
            return ""
        if text[-1] not in ".!?":
            return f"{text}."
        return text

    def _normalize_issue_phrase(self, hook_line: Optional[str]) -> str:
        if not hook_line:
            return "doing everything manually"
        first_segment = re.split(r"[.?!]", hook_line)[0].strip()
        if not first_segment:
            return "doing everything manually"
        lowered = first_segment.lower()
        prefixes = ["still ", "stop ", "start ", "are you ", "is your ", "this ", "ever "]
        for prefix in prefixes:
            if lowered.startswith(prefix):
                first_segment = first_segment[len(prefix):].lstrip()
                break
        return first_segment or "doing everything manually"

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    def _enforce_hook_line(self, script_content: str, hook_outline: Optional[Dict[str, Any]]) -> str:
        if not hook_outline:
            return script_content
        hook_line = (hook_outline.get("sections") or {}).get("hook")
        if not hook_line:
            return script_content
        normalized_hook = self._normalize_text(hook_line)
        comparison_window = script_content[: max(len(hook_line) * 2, 40)]
        if normalized_hook and normalized_hook not in self._normalize_text(comparison_window):
            return f"{hook_line} {script_content}".strip()
        return script_content

    # ------------------------------------------------------------------
    def _build_system_prompt(self, use_case_config: Dict[str, Any], hook_hint: Optional[Dict[str, Any]] = None) -> str:
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

        hook_line = (hook_hint or {}).get("sections", {}).get("hook")
        hook_instruction = ""
        if hook_line:
            tone_hint = (hook_hint or {}).get("tone") or style_info["tone"]
            hook_instruction = (
                f"\nHOOK REQUIREMENT: Use this exact sentence as line 1 without changing punctuation: \"{hook_line}\". "
                f"Maintain the {tone_hint} delivery while keeping Hook → Problem → Solution → CTA order."
            )
        
        return f"""You are an expert copywriter for viral short-form video ads (TikTok, Reels, Shorts).

MISSION: Create a {duration}-second script ({word_target} words) that stops the scroll and drives action.

STRUCTURE (follow this exactly):
1. HOOK (first 3 seconds): Pattern interrupt. Ask a question, state a bold claim, or call out the viewer directly.
2. PROBLEM/CONTEXT (3-7 seconds): The pain point or desire this product solves.
3. SOLUTION (7-{duration-3} seconds): The product as the hero. ONE key benefit (not feature). Make it tangible.
4. CTA (last 3 seconds): Clear, urgent call-to-action.

STYLE: {style_info['tone']}
APPROACH: {style_info['approach']}{hook_instruction}

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
        *,
        hook: Optional[Dict[str, Any]] = None,
        hook_hint: Optional[Dict[str, Any]] = None,
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

        if hook_hint and hook:
            sections = hook_hint.get("sections") or {}
            variant = hook.get("variant") or {}
            parts.extend([
                "",
                "=== APPROVED HOOK (USE VERBATIM) ===",
                f"Hook Type: {hook.get('hook_type', 'n/a')}",
                f"Hook Tone Target: {hook_hint.get('tone', use_case_config.get('style', 'realistic'))}",
                f"Hook Line (must be sentence 1): {sections.get('hook')}",
            ])
            on_screen = variant.get("on_screen")
            visual = variant.get("visual")
            credibility = variant.get("credibility")
            if on_screen:
                parts.append(f"On-Screen Text: {on_screen}")
            if visual:
                parts.append(f"Visual Reference: {visual}")
            if credibility:
                parts.append(f"Credibility Cue: {credibility}")
            parts.append("Instruction: Use the hook line exactly as written, then continue with Problem → Solution → CTA.")

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
    existing_script: Optional[str] = None,
    hook: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return ScriptGenerator(api_key=api_key).generate_script(
        product_data,
        use_case_config,
        existing_script=existing_script,
        hook=hook,
    )
