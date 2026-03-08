"""Hook image generation service powered by FLUX 2 Pro (Black Forest Labs)."""
from __future__ import annotations

import base64
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
    DEFAULT_MODEL_ENDPOINT = os.getenv("FLUX_MODEL_ENDPOINT", "flux-2-klein")
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

            prompt = self._build_image_prompt(product_data, variant, index)
            try:
                image_payload = self._generate_with_flux(prompt)
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
    def _build_image_prompt(self, product_data: Dict[str, Any], variant: Dict[str, Any], index: int) -> str:
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
        return " ".join(details)

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
        if not isinstance(payload, dict):
            return None
        for key in ("id", "task_id", "taskId", "taskID"):
            value = payload.get(key)
            if value:
                return str(value)
        data_block = payload.get("data")
        if isinstance(data_block, dict):
            nested = self._extract_task_id(data_block)
            if nested:
                return nested
        return None

    def _extract_polling_url(self, payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        candidates = [
            payload.get("polling_url"),
            payload.get("pollingUrl"),
            payload.get("status_url"),
            payload.get("statusUrl"),
            self._get_nested_value(payload, "urls.status"),
            self._get_nested_value(payload, "urls.polling"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
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
    def _generate_with_flux(self, prompt: str) -> FluxImagePayload:
        payload = {
            "prompt": prompt,
            "width": 1024,
            "height": 1024,
            "num_images": 1,
            "guidance_scale": 3.5,
            "steps": 28,
        }
        if self.webhook_url and "webhook_url" not in payload:
            payload["webhook_url"] = self.webhook_url

        with self._reserve_queue_slot():
            response_payload = self._perform_flux_request(endpoint=self.model_endpoint, payload=payload)

            image_payload = self._extract_image_payload(response_payload)
            if image_payload.has_data:
                return image_payload

            task_id = self._extract_task_id(response_payload)
            polling_url = self._extract_polling_url(response_payload)
            if not task_id and not polling_url:
                raise ExternalAPIError(
                    "FLUX",
                    "FLUX response did not include an image payload",
                    payload=response_payload,
                )

            if self.webhook_url and task_id:
                try:
                    payload_from_webhook = self._await_webhook_result(task_id, timeout=self.poll_timeout)
                except ExternalAPIError:
                    LOGGER.warning("FLUX webhook timed out for task %s, falling back to polling", task_id)
                    payload_from_webhook = None
                if payload_from_webhook:
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
        url = f"{self.base_url}/{endpoint}"
        headers = self._build_headers()
        response = self.session.post(url, headers=headers, json=payload, timeout=self.create_timeout)
        if response.status_code >= 400:
            self._apply_rate_limit_backoff(response)
            raise self._build_flux_error(response)
        return self._safe_json(response)

    def _poll_flux_task(self, polling_target: str, *, task_id: Optional[str], timeout: float, interval: float) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                payload = self._perform_flux_get(polling_target)
            except NonRetryableAPIError:
                time.sleep(interval)
                continue
            status = self._normalized_status(payload.get("status"))
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
        raise ExternalAPIError("FLUX", "Timed out waiting for FLUX job to finish", payload={"task_id": task_id, "polling_url": polling_target})

    @api_retry(label="flux_image_status", exceptions=(requests.exceptions.RequestException, ExternalAPIError))
    def _perform_flux_get(self, endpoint_or_url: str) -> Dict[str, Any]:
        if endpoint_or_url.startswith("http://") or endpoint_or_url.startswith("https://"):
            url = endpoint_or_url
        else:
            url = f"{self.base_url}/{endpoint_or_url.lstrip('/')}"
        headers = self._build_headers()
        response = self.session.get(url, headers=headers, timeout=max(30.0, self.poll_interval * 4))
        if response.status_code >= 400:
            if response.status_code == 404:
                raise NonRetryableAPIError("FLUX", "Task not found", status_code=404)
            self._apply_rate_limit_backoff(response)
            raise self._build_flux_error(response)
        return self._safe_json(response)

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
            if payload.get("image_url"):
                return FluxImagePayload(url=payload["image_url"])
            if payload.get("url") and payload.get("url").startswith("http"):
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

    def _safe_json(self, response: requests.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}
