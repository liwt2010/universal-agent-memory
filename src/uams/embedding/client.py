"""Embedding provider implementations.

UAMS ships with three concrete providers:

* ``NullEmbeddingProvider`` for tests and offline-mode operation.
* ``SentenceTransformersProvider`` for on-prem / local embedding using
  the ``sentence-transformers`` library.
* ``OpenAICompatibleEmbeddingProvider`` for any OpenAI-shaped remote
  endpoint (OpenAI / MiniMax / ollama / vLLM).

Each provider is wrapped by ``CachedEmbeddingProvider`` which adds an
LRU (and, optionally, a TTL — ``ttl_seconds``) cache. Embedding work
is deterministic per (provider, text), so caching is always safe.

The ``openai`` package is imported lazily inside the OpenAI-compatible
provider. Install with ``pip install 'universal-agent-memory[embeddings]'``
to enable.
"""

from __future__ import annotations

import json
import hashlib
import threading
import time
from typing import Callable, Iterable

from uams.embedding.base import EmbeddingProvider


class NullEmbeddingProvider(EmbeddingProvider):
    """Returns an empty vector for every text. Sentinel for tests."""

    def embed(self, text: str) -> list[float]:  # type: ignore[override]
        return []


class SentenceTransformersProvider(EmbeddingProvider):
    """Sentence-Transformers backed provider (local model)."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers package required. "
                "Install: pip install 'universal-agent-memory[embeddings]'"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self._name = model_name

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def dimension(self) -> int:
        return self._model.get_sentence_embedding_dimension()

    def embed(self, text: str) -> list[float]:  # type: ignore[override]
        vec = self._model.encode([text], normalize_embeddings=True)[0]
        return [float(x) for x in vec]


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible /embeddings endpoint.

    Set ``base_url`` to point at any OpenAI-shaped provider:

    * OpenAI:    ``https://api.openai.com/v1``
    * MiniMax:   ``https://api.minimaxi.com/v1``
    * ollama:    ``http://localhost:11434/v1``
    * vLLM:      ``http://localhost:8000/v1``
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        timeout: float = 30.0,
    ):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "openai package required. "
                "Install: pip install 'universal-agent-memory[llm]'"
            ) from exc
        if not api_key:
            raise ValueError("api_key is required for OpenAICompatibleEmbeddingProvider")
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        # Remote: rely on explicit config; unknown until first response
        # (callers should set ``embedding_dimension`` explicitly for caching stability)
        return 0

    def embed(self, text: str) -> list[float]:  # type: ignore[override]
        resp = self._client.embeddings.create(model=self._model, input=[text])
        return [float(x) for x in resp.data[0].embedding]


class CachedEmbeddingProvider(EmbeddingProvider):
    """LRU cache wrapping any ``EmbeddingProvider``.

    Cache key = SHA-256(inner class name + text). Thread-safe via RLock
    when using the in-process backend.

    For cross-process sharing (e.g. multiple UAMS worker pods), pass
    ``cache_get`` / ``cache_put`` callables. Values are JSON-serialized
    (``[v0, v1, ...]``) at the call layer so the external backend only
    needs to handle strings.

    Optional ``ttl_seconds``: if set, cached entries carry an expiry
    timestamp and stale entries are treated as cache misses on read. By
    default (``ttl_seconds=None``) the cache keeps the original
    forever-cache semantics 鈥?explicit opt-in to enable staleness bounds.
    ``ttl_seconds <= 0`` is treated as ``None`` (defensive).
    """

    def __init__(
        self,
        inner: EmbeddingProvider,
        max_entries: int = 5000,
        cache_get: Callable[[str], str | None] = None,
        cache_put: Callable[[str, str], None] | None = None,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._inner = inner
        self._external = cache_get is not None and cache_put is not None
        # TTL semantics: None or <= 0 means "no expiry" (backward compatible).
        self._ttl = float(ttl_seconds) if (ttl_seconds and ttl_seconds > 0) else None
        self._clock = clock
        if self._external:
            self._cache_get = cache_get
            self._cache_put = cache_put
        else:
            self._max = max(1, int(max_entries))
            self._cache: dict = {}
            self._lock = threading.RLock()

    def _key(self, text: str) -> str:
        payload = f"{self._inner.__class__.__name__}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize(vec: list[float]) -> str:
        return json.dumps(list(vec))

    @staticmethod
    def _deserialize(raw: str) -> list[float]:
        try:
            return [float(x) for x in json.loads(raw)]
        except Exception:
            return []

    # --- TTL envelope helpers ---
    # Without TTL we store the JSON vec verbatim. With TTL we append
    # "|<exp>" so a stale entry is treated as a miss instead of being
    # silently served.

    def _encode(self, raw: str, now: float) -> str:
        if self._ttl is None:
            return raw
        return f"{raw}|{now + self._ttl:.6f}"

    def _decode(self, raw, now):
        if raw is None:
            return None
        if self._ttl is None:
            return raw
        sep = raw.rfind("|")
        if sep < 0:
            return None
        exp_str = raw[sep + 1:]
        try:
            exp = float(exp_str)
        except ValueError:
            return None
        if now >= exp:
            return None
        return raw[:sep]

    def _lookup(self, key, now):
        if self._external:
            raw = self._cache_get(key)
        else:
            with self._lock:
                raw = self._cache.get(key)
        decoded = self._decode(raw, now)
        if decoded is None:
            return None
        return self._deserialize(decoded)

    def _store(self, key, vec, now):
        encoded = self._encode(self._serialize(vec), now)
        if self._external:
            self._cache_put(key, encoded)
        else:
            with self._lock:
                if len(self._cache) >= self._max:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = encoded

    def embed(self, text: str) -> list[float]:  # type: ignore[override]
        key = self._key(text)
        hit = self._lookup(key, self._clock())
        if hit is not None:
            return hit
        result = list(self._inner.embed(text))
        self._store(key, result, self._clock())
        return result

    def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:  # type: ignore[override]
        texts = list(texts)
        if not texts:
            return []
        keys = [self._key(t) for t in texts]
        now = self._clock()
        misses_idx: list[int] = []
        misses_text: list[str] = []
        results: list[list[float] | None] = [None] * len(texts)
        for i, k in enumerate(keys):
            hit = self._lookup(k, now)
            if hit is not None:
                results[i] = hit
            else:
                misses_idx.append(i)
                misses_text.append(texts[i])
        if misses_text:
            new_vectors = self._inner.embed_batch(misses_text)
            write_now = self._clock()
            for j, vec in zip(misses_idx, new_vectors):
                results[j] = list(vec)
                self._store(keys[j], vec, write_now)
        return [r if r is not None else [] for r in results]


__all__ = [
    "NullEmbeddingProvider",
    "SentenceTransformersProvider",
    "OpenAICompatibleEmbeddingProvider",
    "CachedEmbeddingProvider",
]
