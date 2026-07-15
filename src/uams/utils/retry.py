"""Retry mechanism with exponential backoff for UAMS.

Provides resilient operation wrappers for external service calls
(embedding APIs, database writes, network operations).
"""

from __future__ import annotations

import functools
import time
import threading
from typing import Any, Callable, Type

from uams.utils.logging import get_logger

logger = get_logger(__name__)


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
        on_retry: Callable[[Exception, int, float], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
        jitter: bool = True,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.retryable_exceptions = retryable_exceptions
        self.on_retry = on_retry
        self.on_failure = on_failure
        self.jitter = jitter


class RetryStats:
    """Thread-safe statistics for retry operations."""

    def __init__(self):
        self._total_attempts = 0
        self._total_failures = 0
        self._total_retries = 0
        self._lock = threading.Lock()

    def record_attempt(self, success: bool, retries: int) -> None:
        with self._lock:
            self._total_attempts += 1
            if not success:
                self._total_failures += 1
            self._total_retries += retries

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "total_attempts": self._total_attempts,
                "total_failures": self._total_failures,
                "total_retries": self._total_retries,
                "failure_rate": self._total_failures / max(self._total_attempts, 1),
            }


# Global retry statistics
global_retry_stats = RetryStats()


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    jitter: bool = True,
):
    """Decorator that adds exponential backoff retry logic to any function.

    Example:
        @with_retry(max_retries=3, base_delay=1.0)
        def call_embedding_api(text: str) -> List[float]:
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            config = RetryConfig(
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
                exponential_base=exponential_base,
                retryable_exceptions=retryable_exceptions,
                jitter=jitter,
            )
            return retry_call(func, config, args, kwargs)
        return wrapper
    return decorator


def retry_call(
    func: Callable,
    config: RetryConfig,
    args: tuple = (),
    kwargs: dict | None = None,
) -> Any:
    """Execute a function with retry logic."""
    kwargs = kwargs or {}
    last_exception = None
    retries = 0

    for attempt in range(config.max_retries + 1):
        try:
            result = func(*args, **kwargs)
            global_retry_stats.record_attempt(success=True, retries=retries)
            return result
        except config.retryable_exceptions as e:
            last_exception = e
            retries += 1
            if attempt >= config.max_retries:
                break

            # Calculate delay with exponential backoff
            delay = min(
                config.base_delay * (config.exponential_base ** attempt),
                config.max_delay,
            )
            if config.jitter:
                delay *= (0.5 + 0.5 * (hash(str(time.time())) % 1000) / 1000)

            logger.warning(
                "Retry %d/%d for %s in %.2fs after %s: %s",
                attempt + 1, config.max_retries, func.__name__, delay,
                type(e).__name__, str(e)[:200]
            )

            if config.on_retry:
                try:
                    config.on_retry(e, attempt + 1, delay)
                except Exception:
                    pass

            time.sleep(delay)

    # All retries exhausted
    global_retry_stats.record_attempt(success=False, retries=retries)
    logger.error(
        "All %d retries exhausted for %s. Last error: %s",
        config.max_retries, func.__name__, last_exception
    )

    if config.on_failure:
        try:
            config.on_failure(last_exception)
        except Exception:
            pass

    raise last_exception


# Preset configurations for common scenarios
retry_embedding = functools.partial(
    with_retry,
    max_retries=3,
    base_delay=2.0,
    retryable_exceptions=(ConnectionError, TimeoutError, Exception),
)

retry_db_write = functools.partial(
    with_retry,
    max_retries=5,
    base_delay=0.5,
    max_delay=10.0,
    retryable_exceptions=(ConnectionError, Exception),
)

retry_network = functools.partial(
    with_retry,
    max_retries=3,
    base_delay=1.0,
    retryable_exceptions=(ConnectionError, TimeoutError),
)
