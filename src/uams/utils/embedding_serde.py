"""Embedding serialization with backward-compatible read.

Writes use JSON for safety (avoids RCE on untrusted storage).
Reads prefer JSON, fall back to pickle for legacy blobs created
before the v1 hardening. New writes are always JSON; legacy blobs
can be migrated in-place by a future maintenance script and pickle
will be dropped then.
"""

import json
import pickle
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

_EMBEDDING_PICKLE_MARKER = b"\x80"  # pickle protocol header


def serialize_embedding(embedding: Optional[List[float]]) -> Optional[bytes]:
    """Serialize an embedding vector to bytes using JSON (safe).

    Returns None if embedding is None. JSON format is portable,
    debuggable, and immune to RCE on deserialize.
    """
    if embedding is None:
        return None
    return json.dumps(embedding).encode("utf-8")


def deserialize_embedding(blob: Optional[bytes]) -> Optional[List[float]]:
    """Deserialize embedding bytes with backward compat.

    Tries JSON first. If the blob starts with the pickle protocol
    marker (legacy data), falls back to pickle.loads with a logged
    warning so we can track the migration surface.
    """
    if not blob:
        return None
    if isinstance(blob, (bytes, bytearray)) and bytes(blob[:1]) == _EMBEDDING_PICKLE_MARKER:
        logger.warning(
            "Loading legacy pickle-encoded embedding blob; "
            "consider running the storage migration to clear these"
        )
        return pickle.loads(blob)  # noqa: S301 — intentional legacy fallback
    try:
        result = json.loads(blob)
        # Coerce to list[float] defensively
        if isinstance(result, list):
            return [float(x) for x in result]
        logger.warning("Embedding JSON decoded to non-list type: %s", type(result).__name__)
        return None
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # Last resort: legacy pickle (without marker — corrupt or older)
        logger.warning("JSON decode failed for embedding blob (%s); trying pickle", exc)
        try:
            return pickle.loads(blob)  # noqa: S301 — intentional legacy fallback
        except Exception:
            logger.exception("Failed to deserialize embedding blob (json + pickle both failed)")
            return None


__all__ = ["serialize_embedding", "deserialize_embedding"]
