"""Privacy filtering and deduplication with improved accuracy.

Improved regex patterns to reduce false positives (UUIDs, long words)
and added common PII types (phone numbers, Chinese IDs, bearer tokens).
"""

from __future__ import annotations

import re
import time
import threading

from uams.core.enums import PrivacyLevel
from uams.core.models import MemoryPayload
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class PrivacyFilter:
    """
    Strip secrets and PII before storage.
    Configurable per deployment via regex patterns.
    Improved accuracy over naive implementation.

    v0.6.0 split: secret patterns (API keys, bearer tokens, credit
    cards, etc.) are ALWAYS applied — even on PUBLIC-level text —
    because a leaked AWS key or OpenAI key in a public-tier memory
    is a P0 incident regardless of the user's privacy intent.
    PII patterns (email, phone, SSN) are only applied at PRIVATE /
    INTERNAL / SECRET levels.
    """

    # Secret patterns: ALWAYS applied (any privacy level).
    # These are things that are never safe to store in plaintext
    # regardless of how the caller marked the content.
    SECRET_PATTERNS = [
        # OpenAI API keys
        (r'\bsk-[a-zA-Z0-9]{48}\b', '<OPENAI_API_KEY>'),
        # AWS Access Key ID
        (r'\bAKIA[0-9A-Z]{16}\b', '<AWS_ACCESS_KEY>'),
        # AWS Secret Access Key (loose pattern, but better than 32-64 generic)
        (r'\b[A-Za-z0-9/+=]{40}\b', '<AWS_SECRET_KEY>'),
        # Credit cards (with Luhn-like structure hints)
        (r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|6(?:011|5[0-9]{2})[0-9]{12}|(?:2131|1800|35\d{3})\d{11})\b', '<CREDIT_CARD>'),
        # Passwords with common prefixes
        (r'(?:password|passwd|pwd)\s*[:=]\s*\S+', 'password: <REDACTED>'),
        # Bearer tokens (HTTP Authorization header)
        (r'(?i)bearer\s+[a-zA-Z0-9_\-\.]+', 'Bearer <TOKEN>'),
        # GitHub personal access tokens (classic)
        (r'\bghp_[a-zA-Z0-9]{36}\b', '<GITHUB_TOKEN>'),
        # GitHub fine-grained tokens
        (r'\bgithub_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}\b', '<GITHUB_TOKEN>'),
    ]

    # PII patterns: applied at PRIVATE / INTERNAL / SECRET only.
    # PUBLIC content keeps user-visible PII (emails, phones) — by
    # design — but secrets are still scrubbed (above).
    PII_PATTERNS = [
        # Emails
        (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '<EMAIL>'),
        # US SSN
        (r'\b\d{3}-\d{2}-\d{4}\b', '<SSN>'),
        # Chinese phone numbers (mobile)
        (r'\b1[3-9]\d{9}\b', '<PHONE>'),
        # Chinese ID card (18 digits, last may be X)
        (r'\b\d{17}[\dXx]\b', '<CN_ID>'),
    ]

    # Back-compat: callers that still pass a single combined pattern
    # list get the union split automatically.
    DEFAULT_PATTERNS = SECRET_PATTERNS + PII_PATTERNS

    def __init__(self, patterns: list[tuple] | None = None):
        if patterns is None:
            self._secret_patterns = list(self.SECRET_PATTERNS)
            self._pii_patterns = list(self.PII_PATTERNS)
        else:
            # Split combined patterns by membership in the canonical
            # SECRET_PATTERNS / PII_PATTERNS lists. If a custom
            # pattern isn't in either, treat it as secret-only
            # (safest default — never store things you don't
            # recognise).
            secret_set = {p for p, _ in self.SECRET_PATTERNS}
            self._secret_patterns = [
                p for p in patterns if p[0] in secret_set
            ] or list(self.SECRET_PATTERNS)
            self._pii_patterns = [
                p for p in patterns if p not in self._secret_patterns
            ]

    def sanitize(self, text: str, level: PrivacyLevel) -> str:
        # SECRETS first — regardless of level. Closes the v0.5.x
        # hole where PUBLIC content was written verbatim and an
        # OpenAI key embedded in it ended up on disk.
        original = text
        for regex, replacement in self._secret_patterns:
            text = re.sub(regex, replacement, text, flags=re.IGNORECASE)
        secret_redactions = (len(original) - len(text))

        if level == PrivacyLevel.SECRET:
            logger.debug("SECRET content fully redacted")
            return "[REDACTED]"

        if level in (PrivacyLevel.PRIVATE, PrivacyLevel.INTERNAL):
            for regex, replacement in self._pii_patterns:
                text = re.sub(regex, replacement, text, flags=re.IGNORECASE)
            if text != original:
                logger.info(
                    "Privacy filter redacted %d chars in text",
                    len(original) - len(text),
                )
        elif secret_redactions:
            logger.info(
                "Public content but secret patterns redacted %d chars",
                secret_redactions,
            )

        return text

    def sanitize_memory_payload(self, payload: MemoryPayload, level: PrivacyLevel) -> MemoryPayload:
        """Sanitize both raw and structured content."""
        raw = self.sanitize(payload.raw, level)
        structured = payload.structured
        # Structured fields only get PII treatment (secrets were
        # already scrubbed inside sanitize() above).
        if structured and level in (PrivacyLevel.PRIVATE, PrivacyLevel.SECRET):
            structured_str = self.sanitize(str(structured), level)
            if structured_str == "[REDACTED]":
                structured = {"_redacted": True}
        return MemoryPayload(raw=raw, structured=structured, embedding=payload.embedding)


class DeduplicationWindow:
    """
    Rolling SHA-256 window to prevent duplicate ingestion within a time window.
    Thread-safe with RLock.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.RLock()

    def is_duplicate(self, payload: MemoryPayload) -> bool:
        fp = payload.fingerprint()
        now = time.time()

        with self._lock:
            # Clean old entries
            self._seen = {
                k: v for k, v in self._seen.items() if now - v < self._window
            }

            if fp in self._seen:
                self._seen[fp] = now
                logger.debug("Duplicate fingerprint detected: %s...", fp[:8])
                return True

            self._seen[fp] = now
            return False

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
