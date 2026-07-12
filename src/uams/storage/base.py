"""Abstract base class for memory stores."""

from abc import ABC, abstractmethod
from typing import Any, List, Optional

from uams.core.models import Memory


class MemoryStore(ABC):
    """
    Abstract storage interface for a single memory tier.

    Implementations may use in-memory dicts, SQLite, ChromaDB, Neo4j, etc.

    Lifecycle:
        Custom subclasses MUST implement ``close()`` — it is called by
        ``UniversalMemorySystem.shutdown()`` to release connections,
        file handles, and pools. Skipping this leaks resources and can
        leave SQLite WAL files unflushed.
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

    @abstractmethod
    def close(self) -> None:
        """Release all resources (connections, file handles, pools).

        Called by ``UniversalMemorySystem.shutdown()``. Must be
        idempotent — shutdown may call it more than once on the same
        instance. Should NOT raise on already-closed resources.
        """
        ...

    @abstractmethod
    def count(self) -> int:
        """Return the total number of memories in this tier.

        Must be O(1) or O(1) round-trip — do NOT materialize the full
        result set to count it. Implementations should use native
        COUNT queries (SQLite / PostgreSQL), DBSIZE / SCAN
        cardinality (Redis), ``collection.count()`` (ChromaDB),
        ``MATCH (n) RETURN count(n)`` (Neo4j), or in-process dict size
        (InMemory). This replaces the previous O(N)
        ``len(list_all(limit=999999))`` pattern in
        ``UniversalMemorySystem.get_stats()``.
        """
        ...

    @abstractmethod
    def delete_by_filter(self, field: str, value: Any) -> int:
        """Delete all memories whose ``context.<field>`` equals ``value``.

        ``field`` is the dotted path inside ``Memory.context`` (e.g.
        ``"agent_id"``, ``"project_id"``, ``"user_id"``). For JSON
        backends the implementation should use a native indexed query
        (``WHERE json_extract(context, '$.<field>') = ?`` on SQLite,
        ``WHERE context->>'<field>' = ?`` on PostgreSQL) so the
        operation is O(matches) rather than O(table).

        Returns the count of memories deleted (0 if no matches).

        Implementations should swallow per-row failures and continue,
        but accumulate them so the count reflects actual deletions.
        """
        ...
