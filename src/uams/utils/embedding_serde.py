"""Embedding serialization — JSON-only, fail-secure on deserialization.

Writes always use JSON (portable, debuggable, immune to RCE on deserialize).
Reads accept JSON only. Legacy pickle blobs are rejected with a clear error
so an operator can decide whether to write a one-off migration script for
their specific deployment. The previous "fall back to pickle.loads if JSON
decode fails" behaviour was a remote code execution vector — if an attacker
could write a single row to a shared store (PostgreSQL, Redis, SQLite file),
the next ``retrieve()`` on that row would execute arbitrary Python. We no
longer accept that risk to support legacy data; the upgrade path is
documented below.

Migration from pickle to JSON
=============================
If you have pre-v0.4.0 data with pickle-encoded embeddings, run a one-off
migration script before deploying this version::

    import pickle
    from uams.utils.embedding_serde import serialize_embedding, deserialize_embedding

    # Pseudocode: SELECT id, embedding_blob FROM memories WHERE embedding_blob LIKE '\\x80%';
    for mid, blob in rows_with_pickle_embeddings:
        try:
            data = pickle.loads(blob)   # one-time only, on controlled data
            new_blob = serialize_embedding(data)
            cursor.execute("UPDATE ... SET embedding = %s WHERE id = %s", (new_blob, mid))
        except Exception:
            log.warning("could not migrate embedding for %s", mid)

After running the migration once, this version's strict JSON-only reader
will accept all rows safely.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


def serialize_embedding(embedding: Optional[List[float]]) -> Optional[bytes]:
    """Serialize an embedding vector to bytes using JSON.

    Returns None if embedding is None. JSON format is portable,
    debuggable, and immune to RCE on deserialize.
    """
    if embedding is None:
        return None
    return json.dumps(embedding).encode("utf-8")


def deserialize_embedding(blob: Optional[bytes]) -> Optional[List[float]]:
    """Deserialize embedding bytes.

    Accepts JSON-encoded embeddings only. Legacy pickle blobs (the byte
    marker ``\\x80``) and corrupt bytes are rejected — ``retrieve()`` will
    return a Memory whose embedding is ``None`` and the operator will see
    a clear ERROR log line.

    If you have pre-v0.4.0 data, run the migration script documented in
    the module docstring before deploying.

    Notes on input type coercion:
      - ``None`` / empty bytes → ``None``
      - ``memoryview`` (psycopg2 BYTEA) → coerced to ``bytes`` first
      - ``bytearray`` (sqlite3.Binary) → coerced via ``bytes(...)``
    """
    if not blob:
        return None
    # psycopg2 returns memoryview for binary columns; coerce to bytes first
    # so json.loads / bytes-prefix slicing all work uniformly.
    if isinstance(blob, memoryview):
        blob = bytes(blob) if hasattr(blob, "tobytes") else blob.tobytes()
    if isinstance(blob, bytearray):
        blob = bytes(blob)

    # Reject legacy pickle-encoded blobs explicitly. This is the
    # fail-secure path — we previously fell back to pickle.loads here,
    # which was an RCE vector. Operators should run the migration
    # script in the module docstring to convert any pre-existing
    # pickle blobs to JSON before deploying this version.
    if isinstance(blob, bytes) and len(blob) >= 1 and blob[:1] == b"\x80":
        logger.error(
            "Refusing to deserialize legacy pickle-encoded embedding blob. "
            "Run the embedding migration script (see docstring of "
            "uams.utils.embedding_serde) before deploying this version. "
            "Treating embedding as None."
        )
        return None

    try:
        result = json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "Failed to JSON-decode embedding blob (%s); treating as None. "
            "This usually means the blob is corrupt or written by a newer "
            "version of UAMS using an incompatible format.",
            exc,
        )
        return None

    # Coerce to list[float] defensively
    if isinstance(result, list):
        try:
            return [float(x) for x in result]
        except (TypeError, ValueError) as exc:
            logger.warning("Embedding JSON contained non-numeric values (%s); treating as None", exc)
            return None
    logger.warning("Embedding JSON decoded to non-list type: %s; treating as None", type(result).__name__)
    return None


__all__ = ["serialize_embedding", "deserialize_embedding"]
