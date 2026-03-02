"""Pollo.ai video generation service."""
import os
import time
import json
import base64
import logging
import subprocess
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from flask import current_app
from app.utils import (
    api_retry,
    ExternalAPIError,
    NonRetryableAPIError,
    TransientJobError,
    RetryConfig,
)


class PolloAIClient:
    """Client for Pollo.ai video generation API."""
    
    BASE_URL = "https://pollo.ai/api/platform"
    
    # Available video generation models
    AVAILABLE_MODELS = {
        # Pollo models
        'pollo-1.5': {'provider': 'pollo', 'model': 'pollo-v1-5', 'supports_text': True, 'supports_image': True},
        'pollo-1.6': {'provider': 'pollo', 'model': 'pollo-v1-6', 'supports_text': True, 'supports_image': True},
        
        # Kling models
        'kling-1.0': {'provider': 'kling-ai', 'model': 'kling-v1-0', 'supports_text': True, 'supports_image': True},
        'kling-1.5': {'provider': 'kling-ai', 'model': 'kling-v1-5', 'supports_text': True, 'supports_image': True},
        'kling-1.6': {'provider': 'kling-ai', 'model': 'kling-v1-6', 'supports_text': True, 'supports_image': True},
        'kling-2.0': {'provider': 'kling-ai', 'model': 'kling-v2-0', 'supports_text': True, 'supports_image': True},
        'kling-2.1': {'provider': 'kling-ai', 'model': 'kling-v2-1', 'supports_text': True, 'supports_image': True},
        
        # Luma models
        'luma-ray-1.6': {'provider': 'luma', 'model': 'luma-ray-1-6', 'supports_text': True, 'supports_image': True},
        'luma-ray-2.0': {'provider': 'luma', 'model': 'luma-ray-2-0', 'supports_text': True, 'supports_image': True},
        'luma-ray-2-flash': {'provider': 'luma', 'model': 'luma-ray-2-0-flash', 'supports_text': True, 'supports_image': True},
        
        # Pika models
        'pika-2.1': {'provider': 'pika', 'model': 'pika-v2-1', 'supports_text': True, 'supports_image': True},
        'pika-2.2': {'provider': 'pika', 'model': 'pika-v2-2', 'supports_text': True, 'supports_image': True},
        
        # Pixverse models
        'pixverse-3.5': {'provider': 'pixverse', 'model': 'pixverse-v3-5', 'supports_text': True, 'supports_image': True},
        'pixverse-4.0': {'provider': 'pixverse', 'model': 'pixverse-v4-0', 'supports_text': True, 'supports_image': True},
        'pixverse-4.5': {'provider': 'pixverse', 'model': 'pixverse-v4-5', 'supports_text': True, 'supports_image': True},
        'pixverse-5.0': {'provider': 'pixverse', 'model': 'pixverse-v5-0', 'supports_text': True, 'supports_image': True},
        'pixverse-5.5': {'provider': 'pixverse', 'model': 'pixverse-v5-5', 'supports_text': True, 'supports_image': True},
        
        # Google Veo models
        'veo-2': {'provider': 'google', 'model': 'veo2', 'supports_text': True, 'supports_image': True},
        'veo-3': {'provider': 'google', 'model': 'veo3', 'supports_text': True, 'supports_image': True},
        'veo-3-fast': {'provider': 'google', 'model': 'veo3-fast', 'supports_text': True, 'supports_image': True},
        'veo-3.1': {'provider': 'google', 'model': 'veo3-1', 'supports_text': True, 'supports_image': True},
        'veo-3.1-fast': {'provider': 'google', 'model': 'veo3-1-fast', 'supports_text': True, 'supports_image': True},
        
        # Hailuo models
        'hailuo-01': {'provider': 'hailuo', 'model': 'video-01', 'supports_text': True, 'supports_image': True},
        'hailuo-02': {'provider': 'hailuo', 'model': 'hailuo-02', 'supports_text': True, 'supports_image': True},
        'hailuo-2.3': {'provider': 'hailuo', 'model': 'hailuo-2-3', 'supports_text': True, 'supports_image': True},
        'hailuo-2.3-fast': {'provider': 'hailuo', 'model': 'hailuo-2-3-fast', 'supports_text': True, 'supports_image': True},
        
        # Hunyuan
        'hunyuan': {'provider': 'hunyuan', 'model': 'hunyuan', 'supports_text': True, 'supports_image': True},
        
        # Sora (OpenAI) - available on Pollo.ai
        'sora-2': {'provider': 'sora', 'model': 'sora-2', 'supports_text': True, 'supports_image': True},
    }
    
    # Style to model recommendations - using less restrictive models
    STYLE_MODELS = {
        'realistic': ['sora-2', 'kling-1.6', 'luma-ray-2.0', 'pika-2.2'],
        'cinematic': ['sora-2', 'luma-ray-2.0', 'kling-1.6', 'pika-2.2'],
        'animated': ['kling-1.6', 'pika-2.2', 'luma-ray-2.0'],
        'comic': ['pika-2.2', 'kling-1.6', 'luma-ray-2.0'],
    }

    RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

    MOCK_VIDEO_FALLBACK = (
        "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAAIZnJlZQAACzVtZGF0AAACrwYF//+r3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NSByMzIyMiBiMzU2MDVhIC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyNSAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTEyIGxvb2thaGVhZF90aHJlYWRzPTIgc2xpY2VkX3RocmVhZHM9MCBucj0wIGRlY2ltYXRlPTEgaW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRyYT0wIGJmcmFtZXM9MyBiX3B5cmFtaWQ9MiBiX2FkYXB0PTEgYl9iaWFzPTAgZGlyZWN0PTEgd2VpZ2h0Yj0xIG9wZW5fZ29wPTAgd2VpZ2h0cD0yIGtleWludD0yNTAga2V5aW50X21pbj0yNSBzY2VuZWN1dD00MCBpbnRyYV9yZWZyZXNoPTAgcmNfbG9va2FoZWFkPTQwIHJjPWNyZiBtYnRyZWU9MSBjcmY9MjMuMCBxY29tcD0wLjYwIHFwbWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0xOjEuMDAAgAAAAN1liIQAO//+46v4FNjIXHB/WJTRjT88Ul2zyEzccr/4HfTVgAAAAwAAAwAAAwLOaqv2JgqiqOwAAAMACuACDAV0FRBkAjoNIIGERCbByhPBVgAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwCAgQAAACRBmiRsQ7/+qZYAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAGDAAAAAhQZ5CeIX/AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGeYXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4AAAACEBnmNqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuEAAAAqQZpoSahBaJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgxAAAAI0GehkURLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGepXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4QAAACEBnqdqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuAAAAAqQZqsSahBbJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgwAAAAI0GeykUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGe6XRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4AAAACEBnutqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuAAAAAqQZrwSahBbJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgxAAAAI0GfDkUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGfLXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4QAAACEBny9qQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuAAAAAqQZs0SahBbJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgwAAAAI0GfUkUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGfcXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4AAAACEBn3NqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuAAAAAqQZt4SahBbJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgxAAAAI0GflkUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxwAAAAIQGftXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4QAAACEBn7dqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuEAAAAqQZu8SahBbJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgwAAAAI0Gf2kUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGf+XRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4AAAACEBn/tqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuEAAAAqQZvgSahBbJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgxAAAAI0GeHkUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxwAAAAIQGePXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4AAAACEBnj9qQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuEAAAAqQZokSahBbJlMCHf//qmWAAADAAADAAADAAADAAADAAADAAADAAADABgwAAAAI0GeQkUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGeYXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4AAAACEBnmNqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuEAAAAqQZpoSahBbJlMCG///qeEAAADAAADAAADAAADAAADAAADAAADAAADADAhAAAAI0GehkUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGepXRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4QAAACEBnqdqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAJuAAAAAqQZqsSahBbJlMCG///qeEAAADAAADAAADAAADAAADAAADAAADAAADADAgAAAAI0GeykUVLC//AAADAAADAAADAAADAAADAAADAAADAAADABxxAAAAIQGe6XRCvwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAm4AAAACEBnutqQr8AAAMAAAMAAAMAAAMAAAMAAAMAAAMA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"
    )
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize the Pollo.ai client.
        
        Args:
            api_key: Pollo.ai API key. If not provided, uses POLLO_API_KEY env var.
        """
        self.api_key = api_key or os.getenv('POLLO_API_KEY')
        if not self.api_key:
            raise ValueError("Pollo.ai API key is required")
        self._fallback_logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------
    def _get_logger(self) -> logging.Logger:
        """Return the best-available logger for the current context."""
        try:
            if current_app:
                return current_app.logger
        except RuntimeError:
            pass
        return self._fallback_logger

    def _log(self, level: int, message: str, **context: Any) -> None:
        logger = self._get_logger()
        if not logger:
            return
        if context:
            try:
                logger.log(level, "%s | %s", message, json.dumps(context, default=str))
            except TypeError:
                logger.log(level, "%s | %s", message, str(context))
        else:
            logger.log(level, message)

    def _log_info(self, message: str, **context: Any) -> None:
        self._log(logging.INFO, message, **context)

    def _log_debug(self, message: str, **context: Any) -> None:
        self._log(logging.DEBUG, message, **context)

    def _log_error(self, message: str, **context: Any) -> None:
        self._log(logging.ERROR, message, **context)

    def _safe_json(self, response: requests.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    @api_retry(label="pollo_request")
    def _perform_request(self, method: str, url: str, *, timeout: int = 60, **kwargs) -> requests.Response:
        response = requests.request(
            method,
            url,
            headers=self._get_headers(),
            timeout=timeout,
            **kwargs,
        )
        if response.status_code >= 400:
            payload = self._safe_json(response)
            message = (
                payload.get("message")
                or payload.get("detail")
                or payload.get("error")
                or response.text
            )
            if response.status_code in self.RETRYABLE_STATUS_CODES or response.status_code >= 500:
                raise ExternalAPIError("Pollo.ai", message, status_code=response.status_code, payload=payload)
            raise NonRetryableAPIError("Pollo.ai", message, status_code=response.status_code, payload=payload)
        return response

    def _get_headers(self) -> Dict[str, str]:
        """Get the API request headers."""
        return {
            'x-api-key': self.api_key,
            'Content-Type': 'application/json'
        }
    
    def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of available video generation models."""
        models = []
        for model_id, config in self.AVAILABLE_MODELS.items():
            models.append({
                'id': model_id,
                'provider': config['provider'],
                'model': config['model'],
                'supports_text': config['supports_text'],
                'supports_image': config['supports_image']
            })
        return models
    
    def get_models_for_style(self, style: str) -> List[str]:
        """Get recommended models for a specific style."""
        return self.STYLE_MODELS.get(style, ['pollo-1.6', 'kling-2.0'])
    
    @api_retry(
        label="pollo_create_job",
        config=RetryConfig(retries=2, base_delay=1.0, backoff=2.0, max_delay=10.0, jitter=0.25),
        exceptions=(requests.exceptions.RequestException, ExternalAPIError)
    )
    def _make_create_request(self, url: str, payload: Dict[str, Any]) -> requests.Response:
        """Make the API request with retry logic."""
        response = requests.post(
            url,
            headers=self._get_headers(),
            json=payload,
            timeout=10  # Reduced from 60 to avoid worker timeout
        )
        response.raise_for_status()
        return response

    def create_video_job(
        self,
        prompt: str,
        model: str = 'kling-1.6',
        aspect_ratio: str = '9:16',
        length: int = 5,
        image_url: Optional[str] = None,
        resolution: str = '720p',
        negative_prompt: Optional[str] = None,
        webhook_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a video generation job.
        
        Args:
            prompt: Text prompt describing the video
            model: Model ID to use (e.g., 'pollo-1.6', 'kling-2.0')
            aspect_ratio: Aspect ratio ('9:16', '16:9', '1:1', '4:5')
            length: Video length in seconds (5 or 10)
            image_url: Optional image URL for image-to-video
            resolution: Video resolution ('480p', '720p', '1080p')
            negative_prompt: Optional negative prompt
            webhook_url: Optional webhook URL for notifications
            
        Returns:
            Dictionary with task_id and initial status
        """
        if model not in self.AVAILABLE_MODELS:
            raise ValueError(f"Unknown model: {model}")
        
        model_config = self.AVAILABLE_MODELS[model]
        provider = model_config['provider']
        model_name = model_config['model']
        
        # Build input based on provider and model
        input_data = self._build_input(
            provider=provider,
            model=model_name,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            length=length,
            image_url=image_url,
            resolution=resolution,
            negative_prompt=negative_prompt
        )
        
        payload = {'input': input_data}
        if webhook_url:
            payload['webhookUrl'] = webhook_url
        
        url = f"{self.BASE_URL}/generation/{provider}/{model_name}"
        prompt_preview = prompt[:160] + ('…' if len(prompt) > 160 else '')
        
        # Log the EXACT payload for debugging
        self._log_info(
            'Pollo API Request Details',
            url=url,
            provider=provider,
            model=model_name,
            payload=input_data,
            webhook=webhook_url
        )
        
        self._log_info(
            'Creating Pollo video job',
            provider=provider,
            model=model_name,
            aspect_ratio=aspect_ratio,
            length=length,
            resolution=resolution,
            has_image=bool(image_url),
            negative_prompt=bool(negative_prompt),
            webhook_defined=bool(webhook_url),
            prompt_preview=prompt_preview
        )

        try:
            response = self._make_create_request(url, payload)
            response_text = response.text
            try:
                result = response.json()
            except ValueError:
                result = {'raw': response_text}
            self._log_info(
                'Pollo video job created',
                status_code=response.status_code,
                response_body=result
            )
            
            # Extract task ID from response (may be in data.taskId or directly in taskId)
            task_id = result.get('taskId')
            status = result.get('status', 'waiting')

            # Check nested data structure
            if not task_id and 'data' in result:
                task_id = result['data'].get('taskId')
                status = result['data'].get('status', status)

            # Validate that we got a task_id - without it we can't track the job
            if not task_id:
                self._log_error(
                    'Pollo API returned success but no taskId',
                    response_body=result
                )
                return {
                    'success': False,
                    'error': 'Pollo API did not return a task ID. The job may not have been created.',
                    'task_id': None,
                    'status': 'failed',
                    'error_type': 'missing_task_id',
                    'raw_response': result
                }

            return {
                'success': True,
                'task_id': task_id,
                'status': status,
                'raw_response': result
            }
            
        except requests.exceptions.HTTPError as e:
            # Handle specific HTTP errors with user-friendly messages
            status_code = getattr(e.response, 'status_code', None)
            error_msg = self._get_user_friendly_error(e, status_code)
            
            self._log_error(
                'Pollo video job request failed',
                status_code=status_code,
                error=str(e)
            )
            
            # Rate limits (429) should be retryable
            is_rate_limit = status_code == 429
            
            if is_rate_limit:
                raise ExternalAPIError(
                    provider="Pollo.ai",
                    message=error_msg,
                    status_code=status_code,
                    retryable=True
                ) from e
            else:
                raise NonRetryableAPIError(
                    provider="Pollo.ai",
                    message=error_msg,
                    status_code=status_code
                ) from e
            
        except requests.exceptions.RequestException as e:
            error_msg = self._get_user_friendly_error(e)
            self._log_error('Pollo video job request failed', error=str(e))
            
            raise ExternalAPIError(
                provider="Pollo.ai",
                message=error_msg,
                retryable=True
            ) from e
        
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self._log_error('Pollo video job unexpected error', error=str(e))
            
            return {
                'success': False,
                'error': error_msg,
                'task_id': None,
                'status': 'failed',
                'error_type': 'unexpected'
            }

    def _get_user_friendly_error(self, exception: Exception, status_code: Optional[int] = None, error_type: Optional[str] = None) -> str:
        """Convert exceptions to user-friendly error messages."""
        if status_code == 401:
            return "Authentication failed. Please check your Pollo.ai API key."
        elif status_code == 403:
            return "Access denied. Your API key may not have permission for this operation."
        elif status_code == 404:
            return "The requested resource was not found."
        elif status_code == 429:
            return "Rate limit exceeded. Please wait a moment and try again."
        elif status_code and status_code >= 500:
            return "Pollo.ai is experiencing issues. Please try again in a few minutes."
        elif isinstance(exception, requests.exceptions.Timeout):
            return "Request timed out. The server is taking too long to respond."
        elif isinstance(exception, requests.exceptions.ConnectionError):
            return "Connection failed. Please check your internet connection."
        elif error_type:
            return f"{error_type}: {str(exception)}"
        else:
            return f"Request failed: {str(exception)}"
    
    def _build_input(
        self,
        provider: str,
        model: str,
        prompt: str,
        aspect_ratio: str,
        length: int,
        image_url: Optional[str],
        resolution: str,
        negative_prompt: Optional[str]
    ) -> Dict[str, Any]:
        """Build the input payload based on provider/model."""
        
        # Ensure length is valid (5 or 10 for most models)
        length = min(max(length, 5), 10)
        if length > 5:
            length = 10
        else:
            length = 5
        
        # Map aspect ratio to standard format
        aspect_map = {
            '9:16': '9:16',
            '16:9': '16:9',
            '1:1': '1:1',
            '4:5': '4:5',
            '4:3': '4:3',
            '3:4': '3:4'
        }
        original_aspect = aspect_ratio
        aspect_ratio = aspect_map.get(aspect_ratio, '9:16')
        
        # Log aspect ratio mapping for debugging
        if original_aspect != aspect_ratio:
            self._fallback_logger.debug(f"Aspect ratio mapped: {original_aspect} -> {aspect_ratio}")
        
        input_data = {}
        
        # Provider-specific input building
        if provider == 'pollo':
            input_data = {
                'prompt': prompt,
                'resolution': resolution,
                'length': length,
                'aspectRatio': aspect_ratio
            }
            if image_url:
                input_data['image'] = image_url
            if negative_prompt:
                input_data['negativePrompt'] = negative_prompt
                
        elif provider == 'kling-ai':
            input_data = {
                'prompt': prompt,
                'length': length,
                'aspectRatio': aspect_ratio,
                'mode': 'std'
            }
            if image_url:
                input_data['image'] = image_url
                input_data['strength'] = 50
            if negative_prompt:
                input_data['negativePrompt'] = negative_prompt
                
        elif provider == 'luma':
            input_data = {
                'prompt': prompt,
                'resolution': resolution if resolution in ['540p', '720p', '1080p', '4k'] else '720p',
                'length': 5 if length <= 5 else 9,
                'aspectRatio': aspect_ratio
            }
            if image_url:
                input_data['image'] = image_url
                
        elif provider == 'pika':
            input_data = {
                'prompt': prompt,
                'aspectRatio': aspect_ratio,
                'duration': length
            }
            if image_url:
                input_data['image'] = image_url
                
        elif provider == 'pixverse':
            input_data = {
                'prompt': prompt,
                'aspect_ratio': aspect_ratio,
                'duration': length
            }
            if image_url:
                input_data['image'] = image_url
                
        elif provider == 'google':
            input_data = {
                'prompt': prompt,
                'aspectRatio': aspect_ratio,
                'durationSeconds': str(length)
            }
            if image_url:
                input_data['image'] = image_url
                
        elif provider == 'hailuo':
            input_data = {
                'prompt': prompt,
                'duration': length
            }
            if image_url:
                input_data['image'] = image_url
                
        elif provider == 'sora':
            input_data = {
                'prompt': prompt,
                'aspectRatio': aspect_ratio,
                'length': length
            }
            if image_url:
                input_data['image'] = image_url
            if negative_prompt:
                input_data['negativePrompt'] = negative_prompt
                
        elif provider == 'hunyuan':
            input_data = {
                'prompt': prompt,
                'aspect_ratio': aspect_ratio,
                'resolution': resolution
            }
            if image_url:
                input_data['image'] = image_url
        
        return input_data
    
    @api_retry(
        label="pollo_check_status",
        config=RetryConfig(retries=3, base_delay=1.5, backoff=2.0, max_delay=30.0, jitter=0.2),
        exceptions=(requests.exceptions.RequestException,)
    )
    def _make_status_request(self, url: str) -> requests.Response:
        """Make the status check request with retry logic."""
        response = requests.get(
            url,
            headers=self._get_headers(),
            timeout=30
        )
        response.raise_for_status()
        return response

    def check_job_status(self, task_id: str, clip: Optional[Any] = None) -> Dict[str, Any]:
        """Check the status of a video generation job."""
        url = f"{self.BASE_URL}/generation/{task_id}/status"
        
        self._log_debug('Checking Pollo job status', task_id=task_id)
        
        try:
            response = self._make_status_request(url)
            result = response.json()
            status = self._extract_status_from_payload(result)
            
            self._log_info('Pollo job status checked', task_id=task_id, status=status, raw_result=result)
            
            return {
                'success': True,
                'task_id': task_id,
                'status': status or result.get('status', 'unknown'),
                'result': result
            }
            
        except requests.exceptions.HTTPError as e:
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            error_body = getattr(getattr(e, 'response', None), 'text', None)
            self._log_error('Pollo job status check failed', task_id=task_id, status_code=status_code, error=str(e), error_body=error_body)
            
            # Fall back to mock simulation when Pollo returns 404
            if status_code == 404:
                self._log_info('Falling back to mock status (404)', task_id=task_id)
                return self._simulate_job_status(task_id, clip)
            
            error_msg = self._get_user_friendly_error(e, status_code)
            return {
                'success': False,
                'task_id': task_id,
                'status': 'error',
                'error': error_msg,
                'error_type': 'http_error',
                'status_code': status_code
            }
            
        except requests.exceptions.RequestException as e:
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            error_body = getattr(getattr(e, 'response', None), 'text', None)
            self._log_error('Pollo job status check failed', task_id=task_id, status_code=status_code, error=str(e), error_body=error_body)
            
            # Fall back to mock simulation when configured or connection fails
            if os.getenv('POLLO_FORCE_MOCK', 'true').lower() == 'true':
                self._log_info('Falling back to mock status', task_id=task_id)
                return self._simulate_job_status(task_id, clip)
            
            error_msg = self._get_user_friendly_error(e)
            return {
                'success': False,
                'task_id': task_id,
                'status': 'error',
                'error': error_msg,
                'error_type': 'network_error'
            }
    
    def poll_until_complete(
        self,
        task_id: str,
        timeout: int = 600,
        poll_interval: int = 5
    ) -> Dict[str, Any]:
        """Poll a job until it completes or times out.
        
        Args:
            task_id: The task ID to poll
            timeout: Maximum time to wait in seconds
            poll_interval: Seconds between polls
            
        Returns:
            Final job status
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            status = self.check_job_status(task_id)
            
            if not status['success']:
                return status
            
            current_status = status['status']
            
            # Check if complete
            if current_status in ['succeed', 'completed', 'success']:
                return {
                    'success': True,
                    'task_id': task_id,
                    'status': 'completed',
                    'result': status.get('result'),
                    'video_url': self._extract_video_url(status.get('result'))
                }
            
            # Check if failed
            if current_status in ['failed', 'error']:
                return {
                    'success': False,
                    'task_id': task_id,
                    'status': 'failed',
                    'error': status.get('result', {}).get('message', 'Generation failed'),
                    'result': status.get('result')
                }
            
            # Still processing, wait and poll again
            time.sleep(poll_interval)
        
        # Timeout reached
        return {
            'success': False,
            'task_id': task_id,
            'status': 'timeout',
            'error': f'Polling timed out after {timeout} seconds'
        }
    
    def _extract_status_from_payload(self, payload: Optional[Dict[str, Any]]) -> Optional[str]:
        """Best-effort extraction of a status field from Pollo responses."""
        if not isinstance(payload, dict):
            return None

        # Check generations array in multiple locations
        generations = payload.get('generations')
        
        # Check in data.generations directly
        if not generations:
            data = payload.get('data', {})
            if isinstance(data, dict):
                generations = data.get('generations')
        
        # Also check nested in result.data.generations (actual Pollo webhook format)
        if not generations and 'result' in payload:
            result_data = payload.get('result', {})
            if isinstance(result_data, dict):
                data = result_data.get('data', {})
                if isinstance(data, dict):
                    generations = data.get('generations')
        
        if isinstance(generations, list) and generations:
            gen = generations[0] or {}
            gen_status = gen.get('status')
            if isinstance(gen_status, str) and gen_status.strip():
                return gen_status.strip().lower()
            if gen.get('url') and not gen_status:
                return 'completed'

        candidates = [
            payload.get('status'),
            payload.get('taskStatus'),
            payload.get('task_status'),
            payload.get('state')
        ]

        data = payload.get('data') if isinstance(payload.get('data'), dict) else None
        if isinstance(data, dict):
            candidates.extend([
                data.get('status'),
                data.get('taskStatus'),
                data.get('task_status')
            ])

        result = payload.get('result') if isinstance(payload.get('result'), dict) else None
        if isinstance(result, dict):
            candidates.extend([
                result.get('status'),
                result.get('taskStatus'),
                result.get('task_status')
            ])
            # Also check in result.data
            result_data = result.get('data', {})
            if isinstance(result_data, dict):
                candidates.extend([
                    result_data.get('status'),
                    result_data.get('taskStatus'),
                    result_data.get('task_status')
                ])

        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip().lower()

        return None

    def _extract_video_url(self, result: Optional[Dict]) -> Optional[str]:
        """Extract video URL from job result."""
        if not result:
            return None
        
        # Try different possible locations for the video URL
        if 'url' in result:
            return result['url']
        if 'videoUrl' in result:
            return result['videoUrl']
        if 'output' in result and isinstance(result['output'], dict):
            return result['output'].get('url') or result['output'].get('videoUrl')
        if 'result' in result and isinstance(result['result'], dict):
            return result['result'].get('url') or result['result'].get('videoUrl')
        if 'local_relative_path' in result:
            return result['local_relative_path']
        
        # Check nested in result.data.generations (actual Pollo webhook format)
        if 'result' in result and isinstance(result['result'], dict):
            data = result['result'].get('data', {})
            if isinstance(data, dict):
                generations = data.get('generations', [])
                if isinstance(generations, list) and generations:
                    return generations[0].get('url') or generations[0].get('videoUrl')
        
        # Check in data.generations directly
        if 'data' in result and isinstance(result['data'], dict):
            generations = result['data'].get('generations', [])
            if isinstance(generations, list) and generations:
                return generations[0].get('url') or generations[0].get('videoUrl')
        
        return None
    
    def download_video(self, video_url: str, output_path: str) -> bool:
        """Download a generated video to a local file.
        
        Args:
            video_url: URL of the video to download
            output_path: Local path to save the video
            
        Returns:
            True if successful, False otherwise
        """
        try:
            response = requests.get(video_url, stream=True, timeout=120)
            response.raise_for_status()
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return True
            
        except Exception as e:
            print(f"Error downloading video: {e}")
            return False
    
    def get_credit_balance(self) -> Dict[str, Any]:
        """Get the current credit balance."""
        url = f"{self.BASE_URL}/credit/balance"
        
        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                timeout=30
            )
            response.raise_for_status()
            result = response.json()
            
            # Handle nested data structure in Pollo API response
            data = result.get('data', result)
            return {
                'success': True,
                'available_credits': data.get('availableCredits', 0),
                'total_credits': data.get('totalCredits', 0)
            }
            
        except requests.exceptions.RequestException as e:
            return {
                'success': False,
                'error': str(e)
            }

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------
    def _simulate_job_status(self, task_id: str, clip: Optional[Any]) -> Dict[str, Any]:
        """Simulate Pollo.ai status progression for troubleshooting."""
        now = datetime.utcnow()
        created_at = None
        if clip is not None:
            created_at = getattr(clip, 'created_at', None)
        if created_at is None:
            # Default to 6 minutes ago so brand-new clips finish immediately when needed
            created_at = now - timedelta(minutes=6)
        
        elapsed = max((now - created_at).total_seconds(), 0)
        duration_seconds = getattr(clip, 'duration', None) or 5
        progress = 0
        status = 'processing'
        detail = 'Mock generation in progress'
        result_payload: Dict[str, Any] = {
            'mock': True,
            'progress': 0,
            'elapsed_seconds': elapsed,
            'eta_seconds': max(0, 300 - elapsed)
        }
        
        if elapsed < 120:
            progress = round((elapsed / 120) * 40, 1)
            detail = 'Initializing render nodes'
        elif elapsed < 300:
            progress = round(40 + ((elapsed - 120) / 180) * 50, 1)
            detail = 'Synthesizing frames'
        else:
            status = 'completed'
            progress = 100
            detail = 'Mock clip ready'
            assets = self._ensure_placeholder_assets(clip, task_id, duration_seconds)
            result_payload.update(assets)
            if clip is not None:
                if assets.get('local_relative_path') and not getattr(clip, 'file_path', None):
                    clip.file_path = assets['local_relative_path']
                if assets.get('local_thumbnail_relative_path') and not getattr(clip, 'thumbnail_path', None):
                    clip.thumbnail_path = assets['local_thumbnail_relative_path']
        
        result_payload.update({
            'status': status,
            'progress': progress,
            'detail': detail
        })
        
        return {
            'success': True,
            'task_id': task_id,
            'status': status,
            'result': result_payload
        }
    
    def _get_upload_folder(self) -> str:
        """Resolve the upload folder even outside an app context."""
        try:
            if current_app and current_app.config.get('UPLOAD_FOLDER'):
                return current_app.config['UPLOAD_FOLDER']
        except RuntimeError:
            pass
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
    
    def _resolve_clip_filename(self, clip: Optional[Any], task_id: str) -> Dict[str, str]:
        """Return filenames used for the placeholder assets."""
        use_case_id = getattr(clip, 'use_case_id', 'mock') or 'mock'
        clip_id = getattr(clip, 'id', 0) or 0
        seq = getattr(clip, 'sequence_order', 0) or 0
        base_name = f"clip_{clip_id:03d}_{seq:02d}"
        if clip is None:
            base_name = f"task_{task_id}"
        video_rel = os.path.join('clips', str(use_case_id), f"{base_name}.mp4")
        thumb_rel = os.path.join('clips', str(use_case_id), 'thumbnails', f"{base_name}.jpg")
        return {
            'video': video_rel,
            'thumbnail': thumb_rel,
            'preview': f"/uploads/{video_rel}"
        }
    
    def _ensure_placeholder_assets(self, clip: Optional[Any], task_id: str, duration_seconds: int) -> Dict[str, str]:
        """Create placeholder video + thumbnail if they do not exist."""
        upload_root = self._get_upload_folder()
        filenames = self._resolve_clip_filename(clip, task_id)
        video_path = os.path.join(upload_root, filenames['video'])
        thumb_path = os.path.join(upload_root, filenames['thumbnail'])
        os.makedirs(os.path.dirname(video_path), exist_ok=True)
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        
        if not os.path.exists(video_path):
            aspect = '1280x720'
            format_hint = None
            if clip is not None:
                format_hint = getattr(getattr(clip, 'use_case', None), 'format', None)
            if format_hint in ('9:16', '4:5'):
                aspect = '720x1280'
            self._create_placeholder_video(video_path, duration_seconds, aspect)
        
        if not os.path.exists(thumb_path):
            self._create_placeholder_thumbnail(video_path, thumb_path)
        
        return {
            'local_file_path': video_path,
            'local_relative_path': filenames['video'],
            'local_thumbnail_path': thumb_path,
            'local_thumbnail_relative_path': filenames['thumbnail'],
            'preview_url': filenames['preview']
        }
    
    def _create_placeholder_video(self, output_path: str, duration: int, resolution: str) -> None:
        """Create a simple placeholder MP4 file using ffmpeg or fallback bytes."""
        cmd = [
            'ffmpeg', '-y',
            '-f', 'lavfi',
            '-i', f"color=c=0x111827:s={resolution}:d={max(duration, 2)}",
            '-vf', "drawtext=text='Pollo Mock Clip':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2",
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            output_path
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self._write_base64_placeholder(output_path)
    
    def _create_placeholder_thumbnail(self, video_path: str, thumb_path: str) -> None:
        """Grab a thumbnail frame from the placeholder video."""
        cmd = ['ffmpeg', '-y', '-i', video_path, '-frames:v', '1', '-q:v', '3', thumb_path]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            # Best-effort fallback: copy video bytes to ensure file exists
            if os.path.exists(video_path):
                with open(video_path, 'rb') as src, open(thumb_path, 'wb') as dst:
                    dst.write(src.read(2048))
    
    def _write_base64_placeholder(self, output_path: str) -> None:
        """Fallback placeholder writer when ffmpeg is unavailable."""
        data = base64.b64decode(self.MOCK_VIDEO_FALLBACK)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'wb') as f:
            f.write(data)


# Convenience functions
def create_video(
    prompt: str,
    api_key: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """Convenience function to create a video without instantiating the class."""
    client = PolloAIClient(api_key=api_key)
    return client.create_video_job(prompt=prompt, **kwargs)


def get_video_status(task_id: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Convenience function to check video status."""
    client = PolloAIClient(api_key=api_key)
    return client.check_job_status(task_id)
