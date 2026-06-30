"""Embedding provider interfaces."""

from typing import Callable, List, Optional

from uams.core.models import MemoryPayload


class EmbeddingProvider:
    """
    Abstract interface for text embedding providers.

    Implementations may use local models (all-MiniLM-L6-v2),
    OpenAI, Gemini, Voyage AI, etc.
    """

    def embed(self, text: str) -> List[float]:
        """Return a dense vector for the given text."""
        raise NotImplementedError

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Return dense vectors for a batch of texts."""
        return [self.embed(t) for t in texts]


class NoOpEmbeddingProvider(EmbeddingProvider):
    """Fallback: returns None/empty, disabling vector search."""

    def embed(self, text: str) -> List[float]:
        return []

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [[] for _ in texts]


# Type alias for convenience
EmbeddingFn = Optional[Callable[[str], List[float]]]
