"""Embedding provider interfaces."""

from __future__ import annotations

from typing import Callable


class EmbeddingProvider:
    """
    Abstract interface for text embedding providers.

    Implementations may use local models (all-MiniLM-L6-v2),
    OpenAI, Gemini, Voyage AI, etc.
    """

    def embed(self, text: str) -> list[float]:
        """Return a dense vector for the given text."""
        raise NotImplementedError

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return dense vectors for a batch of texts."""
        return [self.embed(t) for t in texts]


class NoOpEmbeddingProvider(EmbeddingProvider):
    """Fallback: returns None/empty, disabling vector search."""

    def embed(self, text: str) -> list[float]:
        return []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


# Type alias for convenience
EmbeddingFn = Callable[[str], list[float]] | None
