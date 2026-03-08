"""Hook image generation service powered by FLUX 2 Pro (Black Forest Labs)."""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Callable

import requests
from flask import current_app

from app.utils import api_retry, ExternalAPIError, NonRetryableAPIError

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FLUX webhook + queue coordination helpers (shared between threads/routes)
# ---------------------------------------------------------------------------

_FLUX_QUEUE_LIMIT = 24
_QUEUE_SEMAPHORE = threading.BoundedSemaphore(value=_FLUX_QUEUE_LIMIT)
_WEBHOOK_LOCK = threading.Lock()
_WEBHOOK_RESULTS: Dict[str, Dict[str, Any]] = {}
_WEBHOOK_EVENTS: Dict[str, threading.Event] = {}


_TASK_ID_KEYS = {
    "task_id",
    "taskid",
    "taskID",
    "task-id",
}
_TASK_CONTAINER_KEYS = (
    "task",
    "data",
    "payload",
    "state",
    "record",
    "detail",
    "event",
    "resource",
    "body",
    "entry",
    "message",
)
_TASK_HINT_KEYS = {
    "status",
    "state",
    "result",
    "results",
    "output",
    "outputs",
    "image",
    "images",
    "urls",
    "polling_url",
    "pollingUrl",
    "webhook_url",
    "webhookUrl",
    "created_at",
    "updated_at",
    "progress",
    "kind",
    "type",
}


def extract_flux_task_id(payload: Any) -> Optional[str]:
    """Best-effort extraction of a FLUX task id from arbitrary webhook payloads."""

    stack: List[Any] = [payload]
    seen: set[int] = set()

    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        if isinstance(current, dict):
            for key, value in current.items():
                lowered = key.lower()
                if lowered in _TASK_ID_KEYS or lowered.endswith("task_id"):
                    if isinstance(value, (str, int)) and str(value).strip():
                        return str(value)

            raw_id = current.get("id")
            if raw_id and isinstance(raw_id, (str, int)) and _looks_like_task_container(current):
                return str(raw_id)

            for container_key in _TASK_CONTAINER_KEYS:
                nested = current.get(container_key)
                if isinstance(nested, (dict, list)):
                    stack.append(nested)

            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)

        elif isinstance(current, list):
            for item in current:
                if isinstance(item, (dict, list)):
                    stack.append(item)

    return None


def _looks_like_task_container(payload: Dict[str, Any]) -> bool:
    return any(key in payload for key in _TASK_HINT_KEYS)



def record_flux_webhook_payload(task_id: str, payload: Dict[str, Any]) -> None:
    """Persist webhook payloads so workers can resume without polling."""

    if not task_id:
        return
    with _WEBHOOK_LOCK:
        _WEBHOOK_RESULTS[task_id] = payload
        event = _WEBHOOK_EVENTS.get(task_id)
    if event:
        event.set()


def _peek_flux_webhook_payload(task_id: str) -> Optional[Dict[str, Any]]:
    with _WEBHOOK_LOCK:
        return _WEBHOOK_RESULTS.get(task_id)


def _consume_flux_webhook_payload(task_id: str) -> Optional[Dict[str, Any]]:
    with _WEBHOOK_LOCK:
        payload = _WEBHOOK_RESULTS.pop(task_id, None)
        event = _WEBHOOK_EVENTS.pop(task_id, None)
    if event and not event.is_set():
        event.set()
    return payload


def _register_flux_webhook_listener(task_id: str) -> threading.Event:
    with _WEBHOOK_LOCK:
        event = _WEBHOOK_EVENTS.get(task_id)
        if not event:
            event = threading.Event()
            _WEBHOOK_EVENTS[task_id] = event
    return event


@dataclass
class FluxImagePayload:
    """Representation of a generated image returned by the FLUX API."""

    url: Optional[str] = None
    b64_json: Optional[str] = None

    @property
    def has_data(self) -> bool:
        return bool(self.url or self.b64_json)


class HookImageGenerator:
    """Generate static hook preview images via FLUX 2 Pro.

    The service builds product-aware prompts, calls the Black Forest Labs API,
    and stores the resulting images inside the uploads directory so routes and
    templates can surface the previews instantly.
    """

    DEFAULT_BASE_URL = os.getenv("FLUX_API_BASE_URL", "https://api.bfl.ai/v1")
    DEFAULT_MODEL_ENDPOINT = os.getenv("FLUX_MODEL_ENDPOINT", "flux-2-pro")
    DEFAULT_POLL_INTERVAL = 0.5  # seconds
    DEFAULT_POLL_TIMEOUT = 90.0  # seconds
    DEFAULT_CREATE_TIMEOUT = 15.0  # seconds
    DEFAULT_QUEUE_WAIT = 10.0  # seconds
    RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
    VISUAL_ANGLES: Sequence[str] = (
        "Hero macro close-up of {product} with dramatic rim lighting, shallow depth of field",
        "Lifestyle mid-shot showing a real person interacting with {product}, natural window light, candid energy",
        "Flat lay of {product} on premium textured surface with clean whitespace for typography"
    )
    HOOK_STYLE_PROMPTS: Dict[str, str] = {
        "problem": "high-contrast storytelling, tension between before and after, cinematic volumetric lighting",
        "bold": "data-rich commercial look, glowing metrics overlays, confident premium tone",
        "status": "contrarian editorial style, moody shadows, precise framing",
        "value": "direct-response inspired studio shot, crisp typography focus, glossy highlights",
        "provocative": "punchy magazine cover aesthetic, dramatic lighting, slight glitch accent",
        "stat": "infographic inspired scene with floating numbers and holographic UI",
        "enemy": "dynamic motion blur, energy reclaim vibe, bold red vs teal palette",
        "direct": "creator-to-camera framing, eye-level perspective, conversational authenticity",
        "default": "premium product photography, 8k detail, studio softbox lighting, commercial look"
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        model_endpoint: Optional[str] = None,
        request_delay: float = 0.12,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        poll_timeout: float = DEFAULT_POLL_TIMEOUT,
        create_timeout: float = DEFAULT_CREATE_TIMEOUT,
        queue_wait_timeout: float = DEFAULT_QUEUE_WAIT,
        webhook_url: Optional[str] = None,
        upload_root: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("FLUX_API_KEY") or os.getenv("BFL_API_KEY")
        if not self.api_key:
            raise ValueError("FLUX_API_KEY (or BFL_API_KEY) is required for image generation")

        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._base_url_candidates = self._derive_base_url_candidates(self.base_url)
        self._has_alternate_base = len(self._base_url_candidates) > 1
        self.model_endpoint = (model_endpoint or self.DEFAULT_MODEL_ENDPOINT).lstrip("/")
        self.request_delay = max(request_delay, 0.12)
        self.poll_interval = max(0.1, poll_interval or self.DEFAULT_POLL_INTERVAL)
        self.poll_timeout = max(10.0, poll_timeout or self.DEFAULT_POLL_TIMEOUT)
        self.create_timeout = max(5.0, create_timeout or self.DEFAULT_CREATE_TIMEOUT)
        self.queue_wait_timeout = max(1.0, queue_wait_timeout or self.DEFAULT_QUEUE_WAIT)
        webhook_value = webhook_url or os.getenv("FLUX_WEBHOOK_URL") or ""
        self.webhook_url = webhook_value.strip() or None
        self.session = session or requests.Session()
        self.upload_root = self._derive_upload_root(upload_root)

    # ------------------------------------------------------------------
    def generate_preview_images(
        self,
        product_data: Dict[str, Any],
        hook_variants: List[Dict[str, Any]],
        upload_folder: str,
        *,
        progress_callback: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
        hook_id: Optional[int] = None,
    ) -> List[str]:
        """Generate static preview images for each hook variant.

        Args:
            product_data: Canonical representation of the product/use case.
            hook_variants: Hook variants returned by ``HookGenerator``.
            upload_folder: Absolute folder where the assets should be saved.
            progress_callback: Optional callable for progress updates.
            hook_id: Optional hook identifier for logging.

        Returns:
            List of relative paths (from the upload root) to the saved images.
        """

        if not hook_variants:
            return []

        os.makedirs(upload_folder, exist_ok=True)

        image_paths: List[str] = []
        total = len(hook_variants)
        for index, variant in enumerate(hook_variants):
            step_message = f"Generating image {index + 1} of {total}"
            if hook_id:
                LOGGER.info("Hook %s: %s", hook_id, step_message)
            else:
                LOGGER.info(step_message)
            if progress_callback:
                progress_callback('image', 'start', {
                    'index': index,
                    'total': total,
                    'message': f"{step_message}..."
                })

            prompt_payload = self._build_image_prompt(product_data, variant, index)
            prompt = prompt_payload.get('prompt') or ''
            input_image = prompt_payload.get('input_image')
            try:
                image_payload = self._generate_with_flux(prompt, input_image=input_image)
                filename = f"hook_variant_{index + 1}.png"
                saved_path = self._save_image_payload(image_payload, upload_folder, filename)
                relative_path = self._to_relative_path(saved_path)
                image_paths.append(relative_path)
                complete_message = f"Image {index + 1} of {total} ready"
                if hook_id:
                    LOGGER.info("Hook %s: %s", hook_id, complete_message)
                else:
                    LOGGER.info(complete_message)
                if progress_callback:
                    progress_callback('image', 'complete', {
                        'index': index,
                        'total': total,
                        'relative_path': relative_path,
                        'message': complete_message
                    })
            except Exception as exc:  # pragma: no cover - network failure
                error_message = f"Image {index + 1} of {total} failed: {exc}"
                LOGGER.exception("Hook %s: %s", hook_id or 'preview', error_message)
                if progress_callback:
                    progress_callback('image', 'error', {
                        'index': index,
                        'total': total,
                        'error': str(exc),
                        'message': error_message
                    })
                raise

            if index < len(hook_variants) - 1:
                time.sleep(self.request_delay)

        return image_paths

    # ------------------------------------------------------------------
    def _build_image_prompt(self, product_data: Dict[str, Any], variant: Dict[str, Any], index: int) -> Dict[str, Any]:
        product_name = product_data.get("name") or product_data.get("brand") or "this product"
        description = (product_data.get("description") or "").strip()
        specs = product_data.get("specifications") or {}
        hook_label = (variant.get("type") or variant.get("formula") or "").lower()
        angle = self.VISUAL_ANGLES[index % len(self.VISUAL_ANGLES)].format(product=product_name)
        style = self._style_for_hook(hook_label)
        visual = variant.get("visual") or variant.get("on_screen") or variant.get("verbal") or "attention-grabbing hero moment"

        color_tokens = self._collect_tokens(specs, ("Color", "Colors", "Palette", "Finish"), max_items=3)
        material_tokens = self._collect_tokens(specs, ("Material", "Materials", "Texture"), max_items=2)
        benefit_tokens = self._collect_tokens(specs, ("Benefit", "Key benefit", "Highlights"), max_items=2)

        details: List[str] = [angle, style, f"Subject: {product_name}"]
        if description:
            details.append(f"Context: {description[:180]}")
        if color_tokens:
            details.append(f"Color palette: {', '.join(color_tokens)}")
        if material_tokens:
            details.append(f"Materials/finish: {', '.join(material_tokens)}")
        if benefit_tokens:
            details.append(f"Promise: {', '.join(benefit_tokens)}")
        if visual:
            details.append(f"Visual focus: {visual}.")

        details.append("Shot on cinema camera, ultra realistic textures, 1024x1024, trending on Behance, high dynamic range, zero text artifacts.")
        prompt_text = " ".join(details)
        return {
            "prompt": prompt_text,
            "input_image": self._select_input_image(product_data),
        }

    def _select_input_image(self, product_data: Dict[str, Any]) -> Optional[str]:
        images = product_data.get("images") or []
        if not isinstance(images, (list, tuple)):
            return None
        for candidate in images:
            url = None
            if isinstance(candidate, dict):
                url = candidate.get("url") or candidate.get("src")
            elif isinstance(candidate, str):
                url = candidate
            if not url:
                continue
            url = str(url).strip()
            if url and url.startswith(("http://", "https://")):
                return url
        return None

    def _style_for_hook(self, hook_label: str) -> str:
        hook_label = hook_label.lower()
        for key, prompt in self.HOOK_STYLE_PROMPTS.items():
            if key == "default":
                continue
            if key in hook_label:
                return prompt
        return self.HOOK_STYLE_PROMPTS["default"]

    def _collect_tokens(self, specs: Dict[str, Any], keys: Sequence[str], max_items: int = 3) -> List[str]:
        tokens: List[str] = []
        for key in keys:
            value = specs.get(key)
            if not value:
                continue
            if isinstance(value, (list, tuple)):
                tokens.extend(str(item).strip() for item in value if item)
            else:
                parts = [part.strip() for part in str(value).replace("/", ",").split(",") if part.strip()]
                tokens.extend(parts)
            if len(tokens) >= max_items:
                break
        return tokens[:max_items]

    # ------------------------------------------------------------------
    @contextmanager
    def _reserve_queue_slot(self):
        if not _QUEUE_SEMAPHORE.acquire(timeout=self.queue_wait_timeout):
            raise ExternalAPIError("FLUX", "FLUX queue is saturated, please retry shortly", payload={"limit": _FLUX_QUEUE_LIMIT})
        try:
            yield
        finally:
            _QUEUE_SEMAPHORE.release()

    def _extract_task_id(self, payload: Dict[str, Any]) -> Optional[str]:
        return extract_flux_task_id(payload)

    def _extract_polling_url(self, payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        # First check common top-level keys
        candidates = [
            payload.get("polling_url"),
            payload.get("pollingUrl"),
            payload.get("status_url"),
            payload.get("statusUrl"),
            payload.get("url"),
            payload.get("sample"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip() and candidate.startswith("http"):
                return candidate.strip()
        # Check nested structures
        nested_candidates = [
            self._get_nested_value(payload, "urls.status"),
            self._get_nested_value(payload, "urls.polling"),
            self._get_nested_value(payload, "result.sample"),
            self._get_nested_value(payload, "result.url"),
        ]
        for candidate in nested_candidates:
            if isinstance(candidate, str) and candidate.strip() and candidate.startswith("http"):
                return candidate.strip()
        return None

    def _build_polling_path(self, task_id: Optional[str]) -> str:
        if not task_id:
            raise ExternalAPIError("FLUX", "Cannot poll FLUX job without polling URL or task id")
        return f"tasks/{task_id}"

    def _await_webhook_result(self, task_id: str, timeout: float) -> Dict[str, Any]:
        if not task_id:
            raise ExternalAPIError("FLUX", "Webhook wait requires a task id")
        existing = _peek_flux_webhook_payload(task_id)
        if existing:
            return _consume_flux_webhook_payload(task_id) or existing
        event = _register_flux_webhook_listener(task_id)
        start = time.time()
        while True:
            elapsed = time.time() - start
            remaining = timeout - elapsed
            if remaining <= 0:
                break
            triggered = event.wait(remaining)
            payload = _consume_flux_webhook_payload(task_id)
            if payload:
                return payload
            if not triggered:
                break
        raise ExternalAPIError("FLUX", "Timed out waiting for webhook callback", payload={"task_id": task_id})

    def _normalized_status(self, value: Any) -> str:
        if not value:
            return ""
        return str(value).strip().lower().replace("_", " ")

    def _get_nested_value(self, payload: Dict[str, Any], path: str) -> Optional[Any]:
        current: Any = payload
        for part in path.split('.'):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def _apply_rate_limit_backoff(self, response: requests.Response) -> None:
        if response.status_code != 429:
            return
        retry_after = response.headers.get("Retry-After")
        delay = 2.0
        if retry_after:
            try:
                delay = max(0.5, min(10.0, float(retry_after)))
            except ValueError:
                pass
        time.sleep(delay)

    # ------------------------------------------------------------------
    def _generate_with_flux(self, prompt: str, *, input_image: Optional[str] = None) -> FluxImagePayload:
        payload = {
            "prompt": prompt,
            "width": 1024,
            "height": 1024,
            "num_images": 1,
            "guidance_scale": 3.5,
            "steps": 28,
            "safety_tolerance": 3,
            "output_format": "png",
            "prompt_upsampling": True,
        }
        if input_image:
            payload["input_image"] = input_image
        if self.webhook_url and "webhook_url" not in payload:
            payload["webhook_url"] = self.webhook_url

        with self._reserve_queue_slot():
            response_payload = self._perform_flux_request(endpoint=self.model_endpoint, payload=payload)

            image_payload = self._extract_image_payload(response_payload)
            if image_payload.has_data:
                return image_payload

            task_id = self._extract_task_id(response_payload)
            polling_url = self._extract_polling_url(response_payload)
            LOGGER.info("FLUX create response pending task_id=%s polling_url=%s webhook_enabled=%s response_keys=%s", task_id, polling_url, bool(self.webhook_url), list(response_payload.keys()))
            if not task_id and not polling_url:
                raise ExternalAPIError(
                    "FLUX",
                    "FLUX response did not include an image payload",
                    payload=response_payload,
                )

            if self.webhook_url and task_id:
                try:
                    LOGGER.info("FLUX awaiting webhook for task %s (timeout %.1fs)", task_id, self.poll_timeout)
                    payload_from_webhook = self._await_webhook_result(task_id, timeout=self.poll_timeout)
                except ExternalAPIError:
                    LOGGER.warning("FLUX webhook timed out for task %s, falling back to polling", task_id)
                    payload_from_webhook = None
                if payload_from_webhook:
                    self._log_flux_payload("webhook_payload", payload_from_webhook)
                    image_payload = self._extract_image_payload(payload_from_webhook)
                    if image_payload.has_data:
                        return image_payload
                    response_payload = payload_from_webhook

            polling_target = polling_url or self._build_polling_path(task_id)
            polled_payload = self._poll_flux_task(
                polling_target,
                task_id=task_id,
                timeout=self.poll_timeout,
                interval=self.poll_interval,
            )
            image_payload = self._extract_image_payload(polled_payload)
            if image_payload.has_data:
                return image_payload

            raise ExternalAPIError(
                "FLUX",
                "FLUX job completed without a usable image",
                payload=polled_payload,
            )

    @api_retry(label="flux_image_request", exceptions=(requests.exceptions.RequestException, ExternalAPIError))
    def _perform_flux_request(self, *, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = self._build_headers()
        candidates = self._candidate_urls(endpoint)
        last_error: Optional[BaseException] = None
        for idx, url in enumerate(candidates, start=1):
            has_more = idx < len(candidates)
            try:
                response = self.session.post(url, headers=headers, json=payload, timeout=self.create_timeout)
                if response.status_code >= 400:
                    self._apply_rate_limit_backoff(response)
                    raise self._build_flux_error(response)
                payload_json = self._safe_json(response)
                self._log_flux_payload(f"create_response attempt={idx}", payload_json)
                if idx > 1:
                    LOGGER.info("FLUX create request succeeded via fallback endpoint %s", url)
                return payload_json
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if not self._should_try_alternate_base(has_more=has_more, error=exc):
                    raise
                LOGGER.warning("FLUX create request attempt %s failed via %s: %s. Trying alternate endpoint...", idx, url, exc)
            except ExternalAPIError as exc:
                last_error = exc
                if not self._should_try_alternate_base(has_more=has_more, error=exc, status_code=exc.status_code):
                    raise
                LOGGER.warning("FLUX create request attempt %s returned HTTP %s via %s. Trying alternate endpoint...", idx, exc.status_code, url)
        if last_error:
            raise last_error
        raise ExternalAPIError("FLUX", "Unable to reach FLUX endpoint")

    def _poll_flux_task(self, polling_target: str, *, task_id: Optional[str], timeout: float, interval: float) -> Dict[str, Any]:
        deadline = time.time() + timeout
        display_target = polling_target if polling_target.startswith("http") else f"{self.base_url}/{polling_target.lstrip('/')}"
        LOGGER.info("FLUX poll start task=%s target=%s timeout=%.1fs interval=%.1fs", task_id or "unknown", display_target, timeout, interval)
        last_status: Optional[str] = None
        while time.time() < deadline:
            try:
                payload = self._perform_flux_get(polling_target, task_id=task_id)
            except NonRetryableAPIError as exc:
                LOGGER.warning("FLUX poll task %s received non-retryable response: %s", task_id or polling_target, exc)
                time.sleep(interval)
                continue
            status = self._normalized_status(payload.get("status"))
            if status != last_status:
                LOGGER.info("FLUX poll task %s status=%s progress=%s", task_id or polling_target, status or "<unknown>", payload.get("progress"))
                last_status = status
            if status in {"ready", "completed", "success", "succeeded"}:
                return payload
            if status in {"pending", "processing", "running"}:
                time.sleep(interval)
                continue
            if status in {"request moderated", "content moderated"}:
                raise NonRetryableAPIError("FLUX", "FLUX request was moderated", payload=payload)
            if status in {"error", "failed"}:
                raise ExternalAPIError("FLUX", "FLUX job failed", payload=payload)
            time.sleep(interval)
        LOGGER.error("FLUX poll timeout task=%s last_status=%s target=%s", task_id or polling_target, last_status, display_target)
        raise ExternalAPIError("FLUX", "Timed out waiting for FLUX job to finish", payload={"task_id": task_id, "polling_url": polling_target, "last_status": last_status})

    @api_retry(label="flux_image_status", exceptions=(requests.exceptions.RequestException, ExternalAPIError))
    def _perform_flux_get(self, endpoint_or_url: str, *, task_id: Optional[str] = None) -> Dict[str, Any]:
        headers = self._build_headers()
        if endpoint_or_url.startswith("http://") or endpoint_or_url.startswith("https://"):
            targets = [endpoint_or_url]
        else:
            targets = self._candidate_urls(endpoint_or_url)
        last_error: Optional[BaseException] = None
        for idx, url in enumerate(targets, start=1):
            has_more = idx < len(targets)
            try:
                params = {"id": task_id} if task_id else None
                response = self.session.get(url, headers=headers, params=params, timeout=max(30.0, self.poll_interval * 4))
                if response.status_code >= 400:
                    error: ExternalAPIError
                    if response.status_code == 404:
                        error = NonRetryableAPIError("FLUX", "Task not found", status_code=404)
                    else:
                        error = self._build_flux_error(response)
                        self._apply_rate_limit_backoff(response)
                    raise error
                payload = self._safe_json(response)
                self._log_flux_payload(f"status_response attempt={idx}", payload)
                if idx > 1:
                    LOGGER.info("FLUX status request succeeded via fallback endpoint %s", url)
                return payload
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if not self._should_try_alternate_base(has_more=has_more, error=exc):
                    raise
                LOGGER.warning("FLUX status request attempt %s failed via %s: %s. Trying alternate endpoint...", idx, url, exc)
            except ExternalAPIError as exc:
                last_error = exc
                if not self._should_try_alternate_base(has_more=has_more, error=exc, status_code=exc.status_code):
                    raise
                LOGGER.warning("FLUX status request attempt %s returned HTTP %s via %s. Trying alternate endpoint...", idx, exc.status_code, url)
        if last_error:
            raise last_error
        raise ExternalAPIError("FLUX", "Unable to fetch FLUX status")

    def _build_headers(self) -> Dict[str, str]:
        return {
            "X-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_flux_error(self, response: requests.Response) -> ExternalAPIError:
        payload = self._safe_json(response)
        message = payload.get("message") or payload.get("detail") or response.text
        retryable = response.status_code in self.RETRYABLE_STATUS or response.status_code >= 500
        if retryable:
            return ExternalAPIError("FLUX", message, status_code=response.status_code, payload=payload)
        return NonRetryableAPIError("FLUX", message, status_code=response.status_code, payload=payload)

    def _extract_image_payload(self, payload: Dict[str, Any]) -> FluxImagePayload:
        # Direct URL responses
        if isinstance(payload, dict):
            # FLUX-specific direct response (sample field)
            if payload.get("sample") and str(payload.get("sample")).startswith("http"):
                return FluxImagePayload(url=payload["sample"])
            if payload.get("image_url"):
                return FluxImagePayload(url=payload["image_url"])
            if payload.get("url") and str(payload.get("url")).startswith("http"):
                return FluxImagePayload(url=payload["url"])

            # OpenAI-style data array
            data = payload.get("data")
            if isinstance(data, list) and data:
                candidate = data[0] or {}
                if candidate.get("url"):
                    return FluxImagePayload(url=candidate["url"])
                if candidate.get("b64_json"):
                    return FluxImagePayload(b64_json=candidate["b64_json"])

            # Nested result/output/image entries
            for key in ("result", "output", "image", "images"):
                value = payload.get(key)
                if isinstance(value, dict):
                    # FLUX uses 'sample' for the image URL
                    if value.get("sample") and str(value.get("sample")).startswith("http"):
                        return FluxImagePayload(url=value["sample"])
                    if value.get("url"):
                        return FluxImagePayload(url=value["url"])
                    if value.get("b64_json"):
                        return FluxImagePayload(b64_json=value["b64_json"])
                    images = value.get("images")
                    if isinstance(images, list) and images:
                        first = images[0] or {}
                        if first.get("url"):
                            return FluxImagePayload(url=first["url"])
                        if first.get("b64_json"):
                            return FluxImagePayload(b64_json=first["b64_json"])
                elif isinstance(value, list) and value:
                    first = value[0] or {}
                    if first.get("url"):
                        return FluxImagePayload(url=first["url"])
                    if first.get("b64_json"):
                        return FluxImagePayload(b64_json=first["b64_json"])

            # Some APIs respond with base64 directly
            if payload.get("b64_json"):
                return FluxImagePayload(b64_json=payload["b64_json"])

        return FluxImagePayload()

    def _save_image_payload(self, payload: FluxImagePayload, folder: str, filename: str) -> str:
        if payload.url:
            return self._download_image(payload.url, folder, filename)
        if payload.b64_json:
            return self._write_base64_image(payload.b64_json, folder, filename)
        raise ExternalAPIError("FLUX", "No image data returned from FLUX API")

    def _download_image(self, url: str, folder: str, filename: str) -> str:
        os.makedirs(folder, exist_ok=True)
        output_path = os.path.join(folder, filename)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        with open(output_path, "wb") as handle:
            handle.write(response.content)
        return output_path

    def _write_base64_image(self, b64_data: str, folder: str, filename: str) -> str:
        os.makedirs(folder, exist_ok=True)
        output_path = os.path.join(folder, filename)
        try:
            raw = base64.b64decode(b64_data)
        except Exception as exc:
            raise ExternalAPIError("FLUX", "Failed to decode base64 image", payload={"error": str(exc)}) from exc
        with open(output_path, "wb") as handle:
            handle.write(raw)
        return output_path

    def _to_relative_path(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.upload_root)
        except ValueError:
            return path

    def _derive_upload_root(self, explicit: Optional[str]) -> str:
        if explicit:
            return os.path.abspath(explicit)
        try:
            if current_app and current_app.config.get("UPLOAD_FOLDER"):
                return os.path.abspath(current_app.config["UPLOAD_FOLDER"])
        except RuntimeError:
            pass
        env_path = os.getenv("UPLOAD_FOLDER")
        if env_path:
            return os.path.abspath(env_path)
        return os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "uploads"))

    def _derive_base_url_candidates(self, primary: str) -> List[str]:
        candidates: List[str] = [primary.rstrip("/")]
        legacy_override = os.getenv("FLUX_LEGACY_BASE_URL")
        if legacy_override:
            legacy = legacy_override.rstrip("/")
            if legacy and legacy not in candidates:
                candidates.append(legacy)
        elif "api.bfl.ai" in primary:
            legacy = primary.replace("api.bfl.ai", "api.bfl.ml").rstrip("/")
            if legacy and legacy not in candidates:
                candidates.append(legacy)
        return candidates

    def _candidate_urls(self, endpoint: str) -> List[str]:
        suffix = endpoint.lstrip("/")
        return [f"{base}/{suffix}" for base in self._base_url_candidates]

    def _should_try_alternate_base(
        self,
        *,
        has_more: bool,
        error: Optional[BaseException] = None,
        status_code: Optional[int] = None,
    ) -> bool:
        if not has_more or not self._has_alternate_base:
            return False
        if isinstance(error, requests.exceptions.RequestException):
            return True
        if isinstance(error, ExternalAPIError):
            return status_code in {404, 421, 426, 308}
        return False

    def _sanitize_flux_payload(self, payload: Any, *, depth: int = 0) -> Any:
        if depth > 4:
            return "<max-depth>"
        redact_keys = {"b64_json", "image_base64", "prompt", "raw"}
        if isinstance(payload, dict):
            sanitized: Dict[str, Any] = {}
            for key, value in payload.items():
                if key in redact_keys:
                    sanitized[key] = "<redacted>"
                    continue
                sanitized[key] = self._sanitize_flux_payload(value, depth=depth + 1)
            return sanitized
        if isinstance(payload, list):
            return [self._sanitize_flux_payload(item, depth=depth + 1) for item in payload[:5]]
        if isinstance(payload, str) and len(payload) > 500:
            return f"{payload[:500]}...<truncated>"
        return payload

    def _format_flux_payload_for_log(self, payload: Any) -> str:
        sanitized = self._sanitize_flux_payload(payload)
        try:
            serialized = json.dumps(sanitized, ensure_ascii=False)
        except Exception:
            serialized = str(sanitized)
        if len(serialized) > 2000:
            serialized = f"{serialized[:2000]}...<truncated>"
        return serialized

    def _log_flux_payload(self, label: str, payload: Any) -> None:
        try:
            message = self._format_flux_payload_for_log(payload)
        except Exception as exc:  # pragma: no cover - defensive logging
            message = f"<failed to serialize payload: {exc}>"
        LOGGER.info("FLUX %s: %s", label, message)


    def _safe_json(self, response: requests.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}
