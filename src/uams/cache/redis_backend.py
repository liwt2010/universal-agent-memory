"""Redis-backed cache backend for cross-process cache sharing.

In multi-process deployments (e.g. several UAMS worker pods), each process
maintains its own in-process LRU cache. The same query + same response
hit will therefore re-run the LLM once per process. ``RedisCacheBackend``
shares cache state across processes via Redis, eliminating redundant
LLM and embedding API calls.

The backend exposes two simple methods (``get`` / ``put``) and is
designed to plug into ``CachedLLMClient`` / ``CachedEmbeddingProvider``
via the ``cache_get`` / ``cache_put`` callables — no changes to those
classes' public API are required.

**Installation**:
    pip install 'universal-agent-memory[redis]'

**Failure semantics**:
    Redis errors (network, timeout, server down) degrade gracefully to
    cache misses — the inner client is invoked normally. The cache
    subsystem must never break the LLM / embedding call path.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


class RedisCacheBackend:
    """Redis-backed key/value cache for ``CachedLLMClient`` / ``CachedEmbeddingProvider``.

    Keys are stored under ``prefix + sha256_hash`` to allow easy scan/cleanup
    via the configured prefix. Values are stored as strings (response bodies).

    Connection is verified at construction via ``PING``. If Redis is
    unreachable, ``RedisCacheBackend`` still constructs but every call
    becomes a no-op (graceful degradation).

    Parameters
    ----------
    host / port / db / password:
        Standard Redis connection params.
    ttl_seconds:
        Optional TTL for cache entries (``EX`` argument).
    key_prefix:
        Prefix applied to every key (allows namespace isolation when
        sharing one Redis instance across services).
    socket_timeout_seconds:
        Timeout for individual Redis operations. Short by default to
        keep cache lookups from blocking the main request path.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        ttl_seconds: float | None = None,
        key_prefix: str = "uams:cache:",
        socket_timeout_seconds: float = 1.0,
    ):
        try:
            import redis  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "redis package required. Install: pip install 'universal-agent-memory[redis]'"
            ) from exc

        self._client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=True,
            socket_connect_timeout=socket_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
        )
        self._ttl = float(ttl_seconds) if ttl_seconds else None
        self._prefix = key_prefix
        self._connected = False

        # Verify connection at construction time; degrade gracefully if down
        try:
            self._client.ping()
            self._connected = True
            logger.info(
                "RedisCacheBackend connected | host=%s port=%d db=%d prefix=%s",
                host, port, db, key_prefix,
            )
        except Exception as exc:
            logger.warning(
                "RedisCacheBackend could not ping Redis (%s); cache disabled until reachable",
                exc,
            )
            self._connected = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Fetch a value by key. Returns ``None`` on miss or any Redis error."""
        if not self._connected:
            return None
        try:
            return self._client.get(self._prefix + key)
        except Exception as exc:
            logger.debug("Redis GET failed (%s); treating as miss", exc)
            return None

    def put(self, key: str, value: str) -> None:
        """Store a value. Silent on any Redis error."""
        if not self._connected:
            return
        try:
            full_key = self._prefix + key
            if self._ttl and self._ttl > 0:
                self._client.set(full_key, value, ex=int(self._ttl))
            else:
                self._client.set(full_key, value)
        except Exception as exc:
            logger.debug("Redis SET failed (%s); cache write skipped", exc)

    def is_connected(self) -> bool:
        return self._connected

    def cache_get_callable(self) -> Callable[[str], str | None]:
        """Return a ``cache_get``-compatible function for ``CachedLLMClient`` etc."""
        return self.get

    def cache_put_callable(self) -> Callable[[str, str], None]:
        """Return a ``cache_put``-compatible function for ``CachedLLMClient`` etc."""
        return self.put


__all__ = ["RedisCacheBackend"]