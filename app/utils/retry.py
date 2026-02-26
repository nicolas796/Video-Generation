"""Retry utilities for network and IO operations."""
from __future__ import annotations

import functools
import random
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Tuple, Type, TypeVar, Union

T = TypeVar("T")

try:  # pragma: no cover - optional dependency guard
    import requests  # type: ignore

    _REQUESTS_EXC: Tuple[type, ...] = (requests.exceptions.RequestException,)
except Exception:  # pragma: no cover - requests not installed
    requests = None  # type: ignore
    _REQUESTS_EXC = tuple()

from .errors import ExternalAPIError



@dataclass(frozen=True)
class RetryConfig:
    """Configuration for retrying operations."""

    retries: int = 3
    base_delay: float = 1.0
    backoff: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.2


def _should_retry(exception: BaseException) -> bool:
    """Return True when an exception instance is retryable."""

    retryable = getattr(exception, "retryable", None)
    if retryable is None:
        return True
    return bool(retryable)


def retry_operation(
    operation: Callable[[], T],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    max_delay: float = 30.0,
    jitter: float = 0.2,
    exceptions: Union[Type[BaseException], Sequence[Type[BaseException]]] = (Exception,),
    logger: Optional[Callable[[str], None]] = None,
    description: Optional[str] = None,
) -> T:
    """Execute ``operation`` with exponential backoff retry logic.

    Args:
        operation: Callable without arguments to execute.
        retries: Maximum number of attempts (>=1).
        base_delay: Initial delay between attempts.
        backoff: Multiplier applied to the delay after every failure.
        max_delay: Cap for the wait duration.
        jitter: Random jitter factor (0-1) added to delays.
        exceptions: Exception types that trigger a retry.
        logger: Optional callable used to log retry events.
        description: Optional label for log messages.

    Returns:
        The result of ``operation`` if it eventually succeeds.

    Raises:
        The last exception raised by ``operation`` when retries are exhausted
        or when the exception is explicitly marked as non-retryable.
    """

    if retries < 1:
        return operation()

    exception_types: Tuple[Type[BaseException], ...]
    if isinstance(exceptions, (tuple, list)):
        exception_types = tuple(exceptions)
    else:
        exception_types = (exceptions,)

    attempt = 0
    while True:
        try:
            return operation()
        except exception_types as exc:  # type: ignore[misc]
            attempt += 1
            if not _should_retry(exc) or attempt > retries:
                raise

            delay = min(max_delay, base_delay * (backoff ** (attempt - 1)))
            jitter_seconds = random.uniform(0, delay * jitter) if jitter > 0 else 0.0
            sleep_time = delay + jitter_seconds

            if logger:
                label = description or getattr(operation, "__name__", "operation")
                logger(
                    f"Retrying {label} after error: {exc}. "
                    f"Attempt {attempt}/{retries} in {sleep_time:.1f}s"
                )

            time.sleep(sleep_time)
        except Exception:
            # Unknown exception: do not retry by default
            raise

def retryable(config: Optional[RetryConfig] = None, *, exceptions: Union[Type[BaseException], Sequence[Type[BaseException]]] = (Exception,)):
    """Decorator alternative for ``retry_operation``."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        def wrapped(*args, **kwargs):
            cfg = config or RetryConfig()
            return retry_operation(
                lambda: func(*args, **kwargs),
                retries=cfg.retries,
                base_delay=cfg.base_delay,
                backoff=cfg.backoff,
                max_delay=cfg.max_delay,
                jitter=cfg.jitter,
                exceptions=exceptions,
                description=func.__name__,
            )

        return wrapped

    return decorator


def api_retry(
    label: Optional[str] = None,
    *,
    config: Optional[RetryConfig] = None,
    exceptions: Optional[Sequence[Type[BaseException]]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for external API calls with sensible defaults."""

    if exceptions:
        default_exceptions: Tuple[Type[BaseException], ...] = tuple(exceptions)
    else:
        combo: Tuple[Type[BaseException], ...] = (ExternalAPIError,)
        if '_REQUESTS_EXC' in globals() and globals()['_REQUESTS_EXC']:
            combo = combo + globals()['_REQUESTS_EXC']  # type: ignore[assignment]
        default_exceptions = combo or (Exception,)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            cfg = config or RetryConfig(retries=4, base_delay=2.0, backoff=2.0, max_delay=45.0, jitter=0.25)
            return retry_operation(
                lambda: func(*args, **kwargs),
                retries=cfg.retries,
                base_delay=cfg.base_delay,
                backoff=cfg.backoff,
                max_delay=cfg.max_delay,
                jitter=cfg.jitter,
                exceptions=default_exceptions,
                description=label or func.__name__,
            )

        return wrapped

    return decorator
