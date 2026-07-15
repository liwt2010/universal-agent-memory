"""Security utilities for UAMS - input sanitization and rate limiting.

Provides defence-in-depth helpers for cleaning user-supplied text before
it reaches persistence layers, plus a process-local rate limiter.

NOTE — what is NOT here, and why
=================================
This module does NOT include a keyword-based "SQL sanitiser" (the old
``sanitize_sql`` was removed in v0.4.1). Keyword denylists are a
well-known anti-pattern that gives false confidence while missing real
attacks (bypass via ``||`` / ``&&``, mixed case, comments, etc.). All UAMS
storage backends use parameterised queries, so this kind of sanitiser
would only add false safety. The ``is_safe_identifier`` helper is
the right tool for validating IDs that DO get interpolated into DDL or
Redis key prefixes (places where parameterised queries don't apply).
"""

from __future__ import annotations

import html
import re
import threading
import time

from uams.utils.logging import get_logger

logger = get_logger(__name__)


class InputValidator:
    """Validate and clean user-supplied text.

    NOTE: ``sanitize_sql`` was removed in v0.4.1 — see module docstring
    for why. If you need to defend against SQL injection, use
    parameterised queries at the storage layer; this module is for
    HTML escaping and length truncation of free-text user input.
    """

    # HTML/XSS patterns. Used by ``sanitize_html`` to escape special
    # characters before text is rendered into HTML pages (e.g. the
    # dashboard in UAMS examples/extensions).
    XSS_PATTERNS = [
        r"<script[^>]*>.*?</script>",
        r"javascript:",
        r"on\w+\s*=\s*['\"]",
        r"<iframe[^>]*>",
        r"<object[^>]*>",
    ]

    @classmethod
    def sanitize_html(cls, text: str) -> str:
        """Escape HTML entities to prevent XSS in browser-facing renders."""
        if not text:
            return text
        return html.escape(text)

    @classmethod
    def validate_length(cls, text: str, max_length: int = 10000,
                        field_name: str = "input") -> str:
        """Truncate text to max_length with a warning log line."""
        if len(text) > max_length:
            logger.warning("%s truncated from %d to %d chars",
                           field_name, len(text), max_length)
            return text[:max_length]
        return text

    @classmethod
    def sanitize_all(cls, text: str, max_length: int = 10000) -> str:
        """Apply HTML escape + length truncation. Suitable for free-text
        user input that may later be rendered in HTML."""
        text = cls.sanitize_html(text)
        text = cls.validate_length(text, max_length)
        return text

    @staticmethod
    def is_safe_identifier(value: str, max_length: int = 256) -> bool:
        """Check that ``value`` is a safe identifier (alnum + dash/underscore).

        Used to validate user-supplied IDs (tenant IDs, project IDs,
        agent IDs) before they're interpolated into PG table names or
        Redis key prefixes — places where parameterised queries don't
        apply. Anything outside [A-Za-z0-9_-] is rejected.

        Replaces the old ``sanitize_sql`` — that keyword denylist
        approach was an anti-pattern. This whitelists a strict
        identifier grammar instead.
        """
        if not value or len(value) > max_length:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9_\-]+", value))


class RateLimiter:
    """Process-local sliding-window rate limiter.

    Thread-safe via an internal RLock. The check-then-append path is
    guarded so two threads cannot both pass the limit and both append,
    which would let through more than ``max_requests`` calls in a
    window under concurrent load. ``InputValidator.rate_limiter()``
    factory is kept for backward compatibility.
    """

    def __init__(self, max_requests: int = 100, window_seconds: float = 60.0):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        """Check if operation is allowed under rate limit.

        Returns ``False`` once ``max_requests`` calls have been made
        for ``key`` within the rolling ``window_seconds`` window.
        """
        now = time.time()
        with self._lock:
            timestamps = self._requests.get(key, [])
            # Drop expired entries first so the list stays bounded.
            timestamps = [t for t in timestamps if now - t < self._window_seconds]
            if len(timestamps) >= self._max_requests:
                logger.warning("Rate limit exceeded for key: %s", key)
                self._requests[key] = timestamps
                return False
            timestamps.append(now)
            self._requests[key] = timestamps
            return True

    def reset(self, key: str) -> None:
        """Reset the rate-limit window for ``key``."""
        with self._lock:
            self._requests.pop(key, None)


# Backward-compatible factory — pre-v0.4.x callers used
# ``InputValidator.rate_limiter(...)`` to get a RateLimiter.
InputValidator.rate_limiter = staticmethod(  # type: ignore[attr-defined]
    lambda max_requests=100, window_seconds=60.0: RateLimiter(
        max_requests=max_requests, window_seconds=window_seconds,
    )
)
