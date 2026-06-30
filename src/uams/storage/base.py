"""Abstract base class for memory stores."""

from abc import ABC, abstractmethod
from typing import Any, List, Optional

from uams.core.models import Memory


class MemoryStore(ABC):
    """
    Abstract storage interface for a single memory tier.

    Implementations may use in-memory dicts, SQLite, ChromaDB, Neo4j, etc.
    """

    @abstractmethod
    def store(self, memory: Memory) -> None:
        """Persist a memory."""
        ...

    @abstractmethod
    def retrieve(self, memory_id: str) -> Optional[Memory]:
        """Retrieve a memory by its ID."""
        ...

    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """Delete a memory by its ID. Returns True if found and deleted."""
        ...

    @abstractmethod
    def search_keywords(self, query: str, k: int = 10) -> List[Memory]:
        """Keyword-based search (BM25 or simple token matching)."""
        ...

    @abstractmethod
    def search_vector(
        self, vector: List[float], k: int = 10, **filters: Any
    ) -> List[Memory]:
        """Vector similarity search (cosine or otherwise)."""
        ...

    @abstractmethod
    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        """Graph traversal starting from an entity or memory ID."""
        ...

    @abstractmethod
    def list_all(self, limit: int = 100) -> List[Memory]:
        """List all memories (for debugging / admin)."""
        ...

    @abstractmethod
    def delete_expired(self) -> int:
        """Delete all memories whose TemporalAnchor has expired. Returns count."""
        ...
