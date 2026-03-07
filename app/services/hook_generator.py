"""Hook generation service powering the Hook stage."""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from openai import OpenAI

from app.utils import api_retry
from app.utils.retry import RetryConfig

LOGGER_NAME = __name__
DEFAULT_MODEL = os.getenv("HOOK_GENERATOR_MODEL", "openai/gpt-5.1-codex")
DEFAULT_RATE_LIMIT_DELAY = float(os.getenv("HOOK_API_DELAY", "0.12"))
HOOK_API_TIMEOUT = float(os.getenv("HOOK_API_TIMEOUT", "15"))
HOOK_API_RETRY_CONFIG = RetryConfig(
    retries=int(os.getenv("HOOK_API_RETRIES", "2")),
    base_delay=float(os.getenv("HOOK_API_BASE_DELAY", "0.75")),
    backoff=float(os.getenv("HOOK_API_BACKOFF", "2.0")),
    max_delay=float(os.getenv("HOOK_API_MAX_DELAY", "3.0")),
    jitter=float(os.getenv("HOOK_API_JITTER", "0.2")),
)


def _clean_sentence(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


@dataclass
class HookVariant:
    """Structured representation of a hook suggestion."""

    type: str
    formula: str
    verbal: str
    on_screen: str
    visual: str
    credibility: str
    why_it_works: str
    best_for: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "formula": self.formula,
            "verbal": self.verbal,
            "on_screen": self.on_screen,
            "visual": self.visual,
            "credibility": self.credibility,
            "why_it_works": self.why_it_works,
            "best_for": self.best_for,
        }


HOOK_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "problem-agitation": {
        "name": "Problem-Agitation Statement",
        "formula": "Still [pain]? You might be making this [niche] mistake.",
        "description": "Names the pain, teases a cause, hints at a fix.",
        "best_for": ["pain-point products", "troubleshooting content", "competitive switch campaigns"],
        "variable_banks": {
            "pains": [
                "paying for clicks that never buy",
                "editing videos for 6 hours",
                "losing warm leads overnight",
                "running out of UGC ideas",
                "guessing which hook actually works",
            ],
            "niches": ["targeting", "creative", "retention", "checkout", "onboarding"],
            "numbers": ["one", "two", "three"],
        },
    },
    "bold-claim": {
        "name": "Bold Claim + Proof Tease",
        "formula": "We [result] in [timeframe]—here's the [duration] rundown.",
        "description": "Specific number + timeframe instantly signals credibility.",
        "best_for": ["case studies", "B2B SaaS", "performance marketing"],
        "variable_banks": {
            "results": ["cut CAC by 37%", "saved $50K", "10x'd output", "doubled demos"],
            "timeframes": ["14 days", "30 days", "the first week"],
            "durations": ["20-second", "30-second", "45-second"],
        },
    },
    "status-quo-flip": {
        "name": "Status-Quo Flip",
        "formula": "Stop [common action]. Start [better action].",
        "description": "Contrarian take that reframes effort toward leverage.",
        "best_for": ["expert positioning", "strategy content"],
        "variable_banks": {
            "common": [
                "fixing CTR",
                "posting daily",
                "discounting",
                "chasing vanity metrics",
                "writing longer scripts",
            ],
            "better": [
                "fixing your first 3 seconds",
                "scripting the hook",
                "raising perceived value",
                "tracking thumb-stop rate",
                "shortening the cold open",
            ],
        },
    },
    "specific-outcome": {
        "name": "Hyper-Specific Outcome",
        "formula": "[Result] without changing [common variable]—only [surprising variable].",
        "description": "Micro outcome with unexpected lever keeps it believable.",
        "best_for": ["optimization tips", "productized services"],
        "variable_banks": {
            "results": [
                "Add 0.8% conversion",
                "Save 2 hours a day",
                "Lift ROAS 18%",
                "Get 3 extra demos",
            ],
            "common": ["your offer", "your budget", "your product", "your funnel"],
            "surprise": ["your opener", "your text overlay", "your hook", "your first shot"],
        },
    },
    "enemy-of-waste": {
        "name": "Enemy of Waste",
        "formula": "Every [unit] costs you. Here's how to get them back.",
        "description": "Personifies waste so the product can be the hero.",
        "best_for": ["automation", "efficiency", "ops", "revops"],
        "variable_banks": {
            "units": [
                "boring first second",
                "ignored follow-up",
                "missed cart reminder",
                "unwatched hook",
                "stalled DM",
            ],
            "reclaim": [
                "hooks",
                "buyers",
                "scroll-stops",
                "pipeline",
                "creators' time",
            ],
        },
    },
    "direct-question": {
        "name": "Direct Question",
        "formula": "[Specific question]?",
        "description": "Forces brain to answer and instantly qualifies viewers.",
        "best_for": ["niche personas", "B2B", "retargeting"],
        "variable_banks": {
            "stems": [
                "Are you still",
                "Founder, are you",
                "Creative lead, tired of",
                "Marketer, is",
                "Agency owners, want to stop",
            ],
            "pains": [
                "losing buyers in second three",
                "guessing which hook will stick",
                "waiting weeks for edits",
                "explaining your value over and over",
                "spending $300 per concept",
            ],
        },
    },
    "provocative": {
        "name": "Provocative Statement",
        "formula": "[Thing] is actually [negative]. Here's why.",
        "description": "Contrarian truth bomb that earns attention.",
        "best_for": ["thought leadership", "category creation"],
        "variable_banks": {
            "things": [
                "Your beautiful website",
                "Posting daily",
                "Cheap hooks",
                "Buying more placements",
                "Long unedited demos",
            ],
            "negatives": [
                "costing you sales",
                "killing your reach",
                "hurting trust",
                "slowing your launch",
                "training viewers to skip",
            ],
        },
    },
    "value-prop": {
        "name": "Value Proposition",
        "formula": "[Benefit] in [timeframe]. Guaranteed.",
        "description": "Straight-to-the-point promise with risk reversal.",
        "best_for": ["bottom-of-funnel", "retargeting", "trial offers"],
        "variable_banks": {
            "benefits": [
                "Launch a studio-quality video",
                "Fix your hook",
                "Ship three creatives",
                "Get scroll-stopping openers",
                "Unlock a winning hook bank",
            ],
            "timeframes": ["5 minutes", "72 hours", "under a week", "14 days"],
        },
    },
    "shocking-stat": {
        "name": "Shocking Statistic",
        "formula": "Did you know that [statistic]?",
        "description": "Data point that disrupts expectations and builds authority.",
        "best_for": ["education", "awareness", "category POV"],
        "variable_banks": {
            "stats": [
                "73% of ecommerce ads fail in the first 3 seconds",
                "90% of ad recall happens in the hook",
                "84% of shoppers say product video convinced them to buy",
                "68% of marketers still reuse the same hook across platforms",
            ],
        },
    },
}


class HookGenerator:
    """Generate hook variants using templates with optional GPT-4o assistance."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = DEFAULT_MODEL,
        offline_fallback: bool = True,
        request_delay: float = DEFAULT_RATE_LIMIT_DELAY,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.offline_fallback = offline_fallback
        # Ensure we respect the 120ms rate limit guidance even if a smaller delay is configured
        self.request_delay = max(0.12, request_delay)
        self.client: Optional[OpenAI] = None
        if self.api_key:
            http_client = httpx.Client(timeout=HOOK_API_TIMEOUT, follow_redirects=True)
            self.client = OpenAI(api_key=self.api_key, http_client=http_client)

    # ------------------------------------------------------------------
    def generate_variants(
        self,
        product_data: Dict[str, Any],
        hook_type: str,
        *,
        count: int = 3,
    ) -> List[Dict[str, Any]]:
        template_key = (hook_type or "problem-agitation").lower()
        if template_key not in HOOK_TEMPLATES:
            raise ValueError(f"Unknown hook type: {hook_type}")

        template = HOOK_TEMPLATES[template_key]
        context = self._extract_product_context(product_data)
        variants: List[Dict[str, Any]] = []

        for idx in range(max(1, count)):
            variant: Optional[HookVariant] = None
            if self.client:
                try:
                    variant = self._generate_with_ai(template_key, template, context, idx)
                except Exception:
                    if not self.offline_fallback:
                        raise
                    variant = None
            if not variant:
                variant = self._generate_with_template(template_key, template, context, idx)

            variants.append(variant.to_dict())

            if self.client and idx < count - 1 and self.request_delay:
                time.sleep(self.request_delay)

        return variants

    # ------------------------------------------------------------------
    @api_retry(label="hook_ai", exceptions=(Exception,), config=HOOK_API_RETRY_CONFIG)
    def _call_completion(self, messages: List[Dict[str, str]], temperature: float = 0.85) -> Dict[str, Any]:
        if not self.client:
            raise RuntimeError("OpenAI client unavailable")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=380,
        )
        return response.model_dump()

    def _generate_with_ai(
        self,
        template_key: str,
        template: Dict[str, Any],
        context: Dict[str, Any],
        variant_index: int,
    ) -> HookVariant:
        if not self.client:
            raise RuntimeError("OpenAI client unavailable")

        system_prompt = (
            "You are a senior direct-response copywriter. "
            "Generate 5-second hook copy using the provided formula. "
            "Return JSON with keys verbal, on_screen, visual, credibility, why_it_works."
        )
        user_prompt = self._build_ai_prompt(template_key, template, context)
        temperature = 0.75 + (variant_index * 0.08)
        payload = self._call_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )
        content = payload["choices"][0]["message"]["content"]
        data = self._parse_ai_content(content)
        return HookVariant(
            type=template["name"],
            formula=template["formula"],
            verbal=data.get("verbal", ""),
            on_screen=data.get("on_screen", data.get("onScreen", "")),
            visual=data.get("visual", ""),
            credibility=data.get("credibility", ""),
            why_it_works=data.get("why_it_works", template["description"]),
            best_for=template.get("best_for", []),
        )

    def _build_ai_prompt(self, template_key: str, template: Dict[str, Any], context: Dict[str, Any]) -> str:
        benefits = ", ".join(context["benefits"][:3]) or context["description"]
        pains = ", ".join(context["pains"][:2])
        differentiators = ", ".join(context["differentiators"][:2])
        stats = ", ".join(context["stats"][:2])

        return (
            f"Product: {context['name']}\n"
            f"Brand: {context['brand']}\n"
            f"Audience: {context['audience']}\n"
            f"Category: {context['category']}\n"
            f"Benefits: {benefits}\n"
            f"Pains: {pains}\n"
            f"Differentiators: {differentiators}\n"
            f"Stats: {stats}\n"
            f"Formula: {template['formula']}\n"
            "Rules:\n"
            "- Verbal hook = 5-10 words\n"
            "- On-screen text = 3-7 words\n"
            "- Visual: describe pattern interrupt or scene\n"
            "- Credibility: mention proof, face, dashboard, or demo\n"
            "- Why it works: explain psychological trigger\n"
            "Respond with strict JSON."
        )

    def _parse_ai_content(self, content: str) -> Dict[str, str]:
        cleaned = content.strip()
        if cleaned.startswith("`"):
            cleaned = cleaned.strip("`\n")
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        result: Dict[str, str] = {
            "verbal": "",
            "on_screen": "",
            "visual": "",
            "credibility": "",
            "why_it_works": "",
        }
        for line in cleaned.splitlines():
            key_value = line.split(":", 1)
            if len(key_value) != 2:
                continue
            key, value = key_value[0].strip().lower(), key_value[1].strip()
            if "verbal" in key:
                result["verbal"] = value
            elif "on" in key:
                result["on_screen"] = value
            elif "visual" in key:
                result["visual"] = value
            elif "cred" in key:
                result["credibility"] = value
            elif "why" in key:
                result["why_it_works"] = value
        return result

    def _generate_with_template(
        self,
        template_key: str,
        template: Dict[str, Any],
        context: Dict[str, Any],
        variant_index: int,
    ) -> HookVariant:
        rng = random.Random()
        rng.seed(hash((template_key, context["name"], variant_index)))
        banks = template.get("variable_banks", {})

        chooser = lambda key, default: self._choose_from_context(rng, context, key, banks.get(key, default), default)

        if template_key == "problem-agitation":
            pain = chooser("pains", banks.get("pains", []))
            niche = chooser("niches", banks.get("niches", []))
            number = rng.choice(banks.get("numbers", ["one"]))
            verbal = f"Still {pain}? You're making this {number} {niche} mistake."
            on_screen = f"Still {pain}?"
            visual = "Split screen between painful 'before' and smooth 'after' shot"
            credibility = "Show creator pointing at analytics overlay"
        elif template_key == "bold-claim":
            result = chooser("results", banks.get("results", []))
            timeframe = chooser("timeframes", banks.get("timeframes", []))
            duration = chooser("durations", banks.get("durations", []))
            verbal = f"We {result} in {timeframe}. Here's the {duration} rundown."
            on_screen = f"{result} in {timeframe}"
            visual = "Overlay of metric dashboard climbing while narrator gestures"
            credibility = "Show actual metric screenshot or testimonial"
        elif template_key == "status-quo-flip":
            common = chooser("common", banks.get("common", []))
            better = chooser("better", banks.get("better", []))
            verbal = f"Stop {common}. Start {better}."
            on_screen = f"Stop {common}".title()
            visual = "Quick jump cut: X mark over old habit, green check on new habit"
            credibility = "Expert talking head with caption"
        elif template_key == "specific-outcome":
            result = chooser("results", banks.get("results", []))
            common = chooser("common", banks.get("common", []))
            lever = chooser("surprise", banks.get("surprise", []))
            verbal = f"{result} without changing {common}—only your {lever}."
            on_screen = f"{result}. No {common} tweak."
            visual = "Macro shot of text overlay being edited"
            credibility = "Show split test or A/B toggling"
        elif template_key == "enemy-of-waste":
            unit = chooser("units", banks.get("units", []))
            verbal = f"Every {unit} costs you. Here's how to get them back."
            on_screen = f"{unit.capitalize()} = $$"
            visual = "Timer counting dollars leaking, then plug the leak"
            credibility = "Overlay of recovered minutes/sales"
        elif template_key == "direct-question":
            stem = chooser("stems", banks.get("stems", []))
            pain = chooser("pains", banks.get("pains", []))
            verbal = f"{stem} {pain}?"
            on_screen = f"{pain}?".capitalize()
            visual = "Creator speaks straight to camera, text bubbles pop"
            credibility = "Callout of persona (" + context["audience"] + ")"
        elif template_key == "provocative":
            thing = chooser("things", banks.get("things", []))
            negative = chooser("negatives", banks.get("negatives", []))
            verbal = f"{thing} is actually {negative}. Here's why."
            on_screen = f"{thing} = {negative}".replace("is", "=")
            visual = "Zoom into website/ad, glitch effect reveals truth"
            credibility = "Show proof (analytics, testimonial) immediately"
        elif template_key == "value-prop":
            benefit = chooser("benefits", banks.get("benefits", []))
            timeframe = chooser("timeframes", banks.get("timeframes", []))
            verbal = f"{benefit} in {timeframe}. Guaranteed."
            on_screen = f"{benefit} in {timeframe}".replace(" ", " ")
            visual = "Countdown timer as product appears"
            credibility = "Guarantee stamp or quote"
        elif template_key == "shocking-stat":
            stat = chooser("stats", banks.get("stats", []))
            verbal = f"Did you know that {stat}?"
            on_screen = stat.split(" ")[0] + "% don't know this"
            visual = "Bold kinetic typography of the stat"
            credibility = "Source callout (internal data/customer study)"
        else:
            verbal = template["formula"]
            on_screen = template["formula"]
            visual = "Creator delivering hook"
            credibility = ""

        return HookVariant(
            type=template["name"],
            formula=template["formula"],
            verbal=_clean_sentence(verbal),
            on_screen=_clean_sentence(on_screen),
            visual=_clean_sentence(visual),
            credibility=_clean_sentence(credibility),
            why_it_works=template["description"],
            best_for=template.get("best_for", []),
        )

    # ------------------------------------------------------------------
    def _choose_from_context(
        self,
        rng: random.Random,
        context: Dict[str, Any],
        key: str,
        template_values: List[str],
        fallback: List[str],
    ) -> str:
        context_map = {
            "pains": context["pains"],
            "benefits": context["benefits"],
            "results": context["metrics"],
            "stats": context["stats"],
            "audience": [context["audience"]],
        }
        candidates = [v for v in context_map.get(key, []) if v]
        if template_values:
            candidates += template_values
        if not candidates:
            candidates = fallback or [context["name"]]
        return rng.choice(candidates)

    def _extract_product_context(self, product_data: Dict[str, Any]) -> Dict[str, Any]:
        specs = product_data.get("specifications") or {}
        description = product_data.get("description") or ""
        benefits = self._collect_benefits(product_data, specs, description)
        pains = self._collect_list(product_data, specs, keys=["Pain", "Pain point", "Frustration"])
        metrics = self._collect_metrics(specs, product_data.get("stats"))
        stats = self._collect_stats(specs, description)
        differentiators = self._collect_list(product_data, specs, keys=["Differentiator", "Why it works", "USP"])
        audience = product_data.get("target_audience") or specs.get("Audience") or product_data.get("brand") or "creators"
        category = specs.get("Category") or specs.get("Type") or "video"

        if not pains and description:
            pains = [description.split(".")[0]]
        if not metrics:
            metrics = benefits[:1]
        if not stats:
            stats = metrics[:1]
        if not differentiators:
            differentiators = benefits[:2]

        return {
            "name": product_data.get("name", "this product"),
            "brand": product_data.get("brand", ""),
            "description": description,
            "benefits": benefits,
            "pains": pains,
            "metrics": metrics,
            "stats": stats,
            "differentiators": differentiators,
            "audience": audience,
            "category": category,
        }

    def _collect_benefits(self, product_data: Dict[str, Any], specs: Dict[str, Any], description: str) -> List[str]:
        keys = [
            "Benefit",
            "Key benefit",
            "Highlights",
            "Why you'll love it",
            "Promise",
        ]
        values = self._collect_list(product_data, specs, keys=keys)
        if not values and description:
            sentences = [s.strip() for s in description.split(".") if len(s.strip()) > 8]
            values.extend(sentences[:2])
        return values or [f"Helps {product_data.get('target_audience', 'creators')} move faster"]

    def _collect_list(
        self,
        product_data: Dict[str, Any],
        specs: Dict[str, Any],
        *,
        keys: List[str],
    ) -> List[str]:
        values: List[str] = []
        for key in keys:
            if key in specs and specs[key]:
                candidate = specs[key]
            else:
                candidate = product_data.get(key)
            if not candidate:
                continue
            if isinstance(candidate, list):
                values.extend([_clean_sentence(str(item)) for item in candidate if item])
            else:
                parts = re.split(r"[\n\r\-•]", str(candidate))
                values.extend([_clean_sentence(part) for part in parts if part.strip()])
        return [v for v in values if v]

    def _collect_metrics(self, specs: Dict[str, Any], stats_field: Optional[Any]) -> List[str]:
        values: List[str] = []
        for key in ("Metric", "Result", "Performance", "Stats"):
            if specs.get(key):
                values.extend(self._ensure_list(specs[key]))
        values.extend(self._ensure_list(stats_field))
        cleaned = []
        for value in values:
            match = re.findall(r"[0-9]+%|\$[0-9]+k?|[0-9]+x", value.lower())
            if match:
                cleaned.append(value)
        return cleaned

    def _collect_stats(self, specs: Dict[str, Any], description: str) -> List[str]:
        stats = self._collect_metrics(specs, None)
        if not stats and description:
            pattern = re.compile(r"(\d+%[^.]+)")
            stats = pattern.findall(description)
        return stats

    def _ensure_list(self, value: Optional[Any]) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [_clean_sentence(str(v)) for v in value if v]
        return [_clean_sentence(str(value))]
