"""Security utilities for UAMS - input sanitization and SQL injection prevention.

Provides defense-in-depth against common injection attacks and data poisoning.
"""

import re
import html
from typing import Optional

from uams.utils.logging import get_logger

logger = get_logger(__name__)


class InputValidator:
    """Validates and sanitizes user inputs to prevent injection attacks."""

    # SQL injection patterns
    SQL_PATTERNS = [
        r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|UNION|TRUNCATE)\b)",
        r"(--|#|/\*|\*/)",
        r"(\bOR\b|\bAND\b)\s+\d+\s*=\s*\d+",
        r"(\bWAITFOR\b|\bDELAY\b|\bSHUTDOWN\b)",
        r"(\bxp_\w+|\bsp_\w+)",
    ]

    # HTML/XSS patterns
    XSS_PATTERNS = [
        r"<script[^>]*>.*?</script>",
        r"javascript:",
        r"on\w+\s*=\s*['\"]",
        r"<iframe[^>]*>",
        r"<object[^>]*>",
    ]

    @classmethod
    def sanitize_sql(cls, text: str) -> str:
        """Sanitize input to prevent SQL injection.
        
        Returns sanitized text with SQL keywords and dangerous characters removed.
        """
        if not text:
            return text
        
        # Check for SQL injection patterns
        dangerous = False
        for pattern in cls.SQL_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                dangerous = True
                break
        
        if dangerous:
            logger.warning("SQL injection pattern detected and sanitized: %s...", text[:100])
            # Remove SQL keywords entirely
            text = re.sub(
                r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|UNION|TRUNCATE|WAITFOR|DELAY|SHUTDOWN|OR|AND)\b",
                "", text, flags=re.IGNORECASE
            )
            # Remove dangerous characters
            text = re.sub(r"[;'\"\-\/\*\(\)#]", " ", text)
            # Collapse multiple spaces
            text = re.sub(r"\s+", " ", text).strip()
        
        return text

    @classmethod
    def sanitize_html(cls, text: str) -> str:
        """Escape HTML entities to prevent XSS."""
        if not text:
            return text
        return html.escape(text)

    @classmethod
    def validate_length(cls, text: str, max_length: int = 10000, field_name: str = "input") -> str:
        """Validate and truncate text length."""
        if len(text) > max_length:
            logger.warning("%s truncated from %d to %d chars", field_name, len(text), max_length)
            return text[:max_length]
        return text

    @classmethod
    def sanitize_all(cls, text: str, max_length: int = 10000) -> str:
        """Apply all sanitization steps in sequence. Truncates at the end."""
        text = cls.sanitize_sql(text)
        text = cls.sanitize_html(text)
        text = cls.validate_length(text, max_length)
        return text


class RateLimiter:
    """Simple in-memory rate limiter for operations."""

    def __init__(self, max_requests: int = 100, window_seconds: float = 60.0):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict = {}  # key -> list of timestamps

    def is_allowed(self, key: str) -> bool:
        """Check if operation is allowed under rate limit."""
        import time
        now = time.time()
        
        if key not in self._requests:
            self._requests[key] = []
        
        # Clean old requests
        self._requests[key] = [
            t for t in self._requests[key]
            if now - t < self._window_seconds
        ]
        
        if len(self._requests[key]) >= self._max_requests:
            logger.warning("Rate limit exceeded for key: %s", key)
            return False
        
        self._requests[key].append(now)
        return True

    def reset(self, key: str) -> None:
        """Reset rate limit for a key."""
        self._requests.pop(key, None)
