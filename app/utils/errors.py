"""Custom exception types for the Product Video Generator."""
from __future__ import annotations

from typing import Any, Optional


class ExternalAPIError(Exception):
    """Raised when an external API returns an error response."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: Optional[int] = None,
        payload: Optional[Any] = None,
        retryable: bool = True,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.payload = payload
        self.retryable = retryable
        super().__init__(f"{provider} API error: {message}")


class TransientJobError(ExternalAPIError):
    """Represents a temporary failure when polling long-running jobs."""

    def __init__(self, provider: str, message: str, *, payload: Optional[Any] = None) -> None:
        super().__init__(provider, message, payload=payload, retryable=True)


class NonRetryableAPIError(ExternalAPIError):
    """Represents an error that should immediately bubble up."""

    def __init__(self, provider: str, message: str, *, status_code: Optional[int] = None, payload: Optional[Any] = None) -> None:
        super().__init__(provider, message, status_code=status_code, payload=payload, retryable=False)
