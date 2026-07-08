"""Embedding provider implementations.

Existing abstract base classes live in ``uams.embedding.base``. This module
adds concrete providers and a cache wrapper:

- ``NoOpEmbeddingProvider``   -- already in base; aliased for convenience
- ``SentenceTransformersProvider`` -- local model (lazy import)
- ``OpenAICompatibleEmbeddingProvider`` -- remote, OpenAI-compatible (MiniMax, etc.)
- ``CachedEmbeddingProvider`` -- LRU cache wrapper around any provider

The ``openai`` and ``sentence-transformers`` packages are imported lazily
inside the constructors, so the rest of ``uams`` does not require them.
Install with ``pip install 'universal-agent-memory[embeddings]'`` or
``pip install 'universal-agent-memory[llm]'`` (for the OpenAI-compatible path).
"""

from __future__ import annotations

import hashlib
import threading
from typing import Iterable, List, Optional

from uams.embedding.base import EmbeddingProvider, NoOpEmbeddingProvider


# Re-export the noop provider under a stable name
NullEmbeddingProvider = NoOpEmbeddingProvider


class SentenceTransformersProvider(EmbeddingProvider):
    """Local sentence-transformers provider.

    Lazy-imports ``sentence_transformers`` so the package is optional.
    Default model ``all-MiniLM-L6-v2`` (384 dims, ~80 MB).
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        device: Optional[str] = None,
        batch_size: int = 32,
    ):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers required. "
                "Install: pip install 'universal-agent-memory[embeddings]'"
            ) from exc
        kwargs = {}
        if device:
            kwargs["device"] = device
        self._model = SentenceTransformer(model_name, **kwargs)
        self._batch_size = max(1, int(batch_size))
        self._model_name = model_name

    def embed(self, text: str) -> List[float]:
        return list(self.embed_batch([text])[0])

    def embed_batch(self, texts: Iterable[str]) -> List[List[float]]:
        texts = list(texts)
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            batch_size=self._batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return int(self._model.get_sentence_embedding_dimension())


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """Remote OpenAI-compatible embedding provider.

    Works with OpenAI, MiniMax, ollama (in OpenAI-compat mode), vLLM, etc.
    Set ``base_url`` to the provider's endpoint.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        timeout: float = 10.0,
        max_retries: int = 2,
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
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        self._model = model
        self._timeout = timeout

    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Iterable[str]) -> List[List[float]]:
        texts = list(texts)
        if not texts:
            return []
        resp = self._client.embeddings.create(
            model=self._model,
            input=texts,
            timeout=self._timeout,
        )
        # OpenAI returns embeddings in input order
        return [list(d.embedding) for d in resp.data]

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        # Remote: rely on explicit config; unknown until first response
        # (callers should set ``embedding_dimension`` explicitly for caching stability)
        return 0


class CachedEmbeddingProvider(EmbeddingProvider):
    """LRU cache wrapping any ``EmbeddingProvider``.

    Cache key = SHA-256(inner class name + text). Thread-safe via RLock.
    """

    def __init__(self, inner: EmbeddingProvider, max_entries: int = 5000):
        self._inner = inner
        self._max = max(1, int(max_entries))
        self._cache: dict = {}
        self._lock = threading.RLock()

    def _key(self, text: str) -> str:
        payload = f"{self._inner.__class__.__name__}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def embed(self, text: str) -> List[float]:
        key = self._key(text)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        result = list(self._inner.embed(text))
        with self._lock:
            if len(self._cache) >= self._max:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    def embed_batch(self, texts: Iterable[str]) -> List[List[float]]:
        # Batch as much as possible; fall back to per-item for misses to reuse cache
        texts = list(texts)
        if not texts:
            return []
        keys = [self._key(t) for t in texts]
        misses_idx: List[int] = []
        misses_text: List[str] = []
        results: List[Optional[List[float]]] = [None] * len(texts)
        with self._lock:
            for i, k in enumerate(keys):
                hit = self._cache.get(k)
                if hit is not None:
                    results[i] = list(hit)
                else:
                    misses_idx.append(i)
                    misses_text.append(texts[i])
        if misses_text:
            new_vectors = self._inner.embed_batch(misses_text)
            with self._lock:
                for j, vec in zip(misses_idx, new_vectors):
                    results[j] = list(vec)
                    key = keys[j]
                    if len(self._cache) >= self._max:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[key] = list(vec)
        return [r if r is not None else [] for r in results]


__all__ = [
    "NullEmbeddingProvider",
    "SentenceTransformersProvider",
    "OpenAICompatibleEmbeddingProvider",
    "CachedEmbeddingProvider",
]