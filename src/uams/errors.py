"""UAMS exception hierarchy.

All exceptions raised from UAMS public surface inherit from
:class:`UAMSError`. Callers can write ``except UAMSError`` to catch
everything UAMS throws, or specific subclasses to branch on error
category.

Internal store layers (``storage/*.py``) keep their existing
``except Exception: logger.exception() + fallback`` pattern for
graceful degradation. The facade (``system.py``) and the
``CascadeForgetter`` boundary translate genuine unrecoverable
failures into the matching subclass so the product layer
(Vault, etc.) can bucket them in Sentry / retry / UI.

Adding a new error category?
    - Subclass :class:`UAMSError`
    - Add to the ``__all__`` tuple
    - Re-export from ``src/uams/__init__.py`` if product callers
      import from the package root
"""

from __future__ import annotations


class UAMSError(Exception):
    """Root of the UAMS exception hierarchy."""


class ConfigError(UAMSError):
    """Raised for invalid or inconsistent ``UAMSConfig`` values.

    Triggered at facade init time after :py:meth:`UAMSConfig.validate`
    has flagged something the validator cannot repair on its own
    (e.g. conflicting backend + TLS settings, or a regex pattern
    that did not compile).
    """


class StorageError(UAMSError):
    """Raised when a backend refuses an operation that the facade
    considers non-recoverable.

    Example: SQLiteStore raises this from :py:meth:`UniversalMemorySystem.observe`
    when the database file is unwritable, after internal retry + WAL
    checkpoint have both failed. Routine "operation failed, fell back"
    events stay as warning logs and do NOT surface as ``StorageError``.
    """


class CascadeError(UAMSError):
    """Raised when ``CascadeForgetter`` cannot complete a cascade.

    Triggered when the visit-set exhausts ``max_depth`` AND the user
    requested ``raise_on_partial=False`` is overridden, or when the
    audit-log writer itself fails to append. Partial-failure cascade
    results continue to be returned via :class:`CascadeReport.failed_ids`
    on the success path.
    """


class LLMError(UAMSError):
    """Raised when an LLM-backed operation cannot proceed and the
    fallback heuristic is unavailable.

    Triggered from ``LLMCompressionEngine.compress`` when
    ``raise_on_failure=True`` AND both the LLM call AND the heuristic
    fallback fail. Default callers (``UniversalMemorySystem``) keep
    ``raise_on_failure=False`` so this is rarely surfaced.
    """


__all__ = [
    "UAMSError",
    "ConfigError",
    "StorageError",
    "CascadeError",
    "LLMError",
]