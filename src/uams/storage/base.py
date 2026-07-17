"""Abstract base class for memory stores."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

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

    v0.6.0 — vector_search_capable: stores that natively support
    vector cosine similarity (ChromaDB, InMemoryStore when an
    embedding_fn is registered) set this class attribute to True.
    Backends without native vector search (SQLite, Redis, PostgreSQL,
    Neo4j) leave it at False; their ``search_vector`` falls back to
    recency-ordered retrieval and logs an INFO-level message so
    operators can see the degraded behaviour instead of getting
    silently wrong results.
    """

    #: Class-level flag. Override to True in stores that implement
    #: real cosine / inner-product similarity in search_vector().
    vector_search_capable: bool = False

    @abstractmethod
    def store(self, memory: Memory) -> None:
        """Persist a memory."""
        ...

    @abstractmethod
    def retrieve(self, memory_id: str) -> Memory | None:
        """Retrieve a memory by its ID."""
        ...

    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """Delete a memory by its ID. Returns True if found and deleted."""
        ...

    @abstractmethod
    def search_keywords(self, query: str, k: int = 10) -> list[Memory]:
        """Keyword-based search (BM25 or simple token matching)."""
        ...

    @abstractmethod
    def search_vector(
        self, vector: list[float], k: int = 10, **filters: Any
    ) -> list[Memory]:
        """Vector similarity search (cosine or otherwise)."""
        ...

    @abstractmethod
    def search_graph(self, entity: str, depth: int = 2) -> list[Memory]:
        """Graph traversal starting from an entity or memory ID."""
        ...

    @abstractmethod
    def list_all(self, limit: int = 100) -> list[Memory]:
        """List all memories (for debugging / admin)."""
        ...

    def list_all_paginated(
        self, limit: int = 1000, offset: int = 0
    ) -> list[Memory]:
        """Pagination-aware variant of list_all.

        v0.6.0: added so ``MigrationTool.migrate()`` and other
        batch callers don't have to materialise the whole collection
        in memory at once. Default implementation uses list_all() +
        in-process offset slicing — safe for stores whose list_all
        already paginates (Chroma, Neo4j, InMemory, Redis with its
        own offset logic). Backends that clamp list_all() to a
        hard cap (SQLite) MUST override this to support true
        OFFSET-based pagination.

        Returns at most ``limit`` rows starting at ``offset``.
        """
        if offset == 0:
            return self.list_all(limit=limit)
        all_rows = self.list_all(limit=10_000)
        return all_rows[offset:offset + limit]

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

    def truncate(self) -> int:
        """Delete every memory in this tier in a single native
        operation. v0.6.0 replaces the previous O(N) pattern of
        ``for mem in list_all(limit=999999): delete(mem.id)`` which
        silently dropped everything past 999 rows on SQLite
        (SQLITE_MAX_VARIABLE_NUMBER cap) and was O(N) round-trips
        on every other backend.

        Returns the count deleted (0 if empty).

        Default implementation: O(N) via list_all + per-row delete,
        safe for InMemoryStore where the data set is in-process.
        Backends with native TRUNCATE / DELETE-FROM semantics MUST
        override for a single round-trip.
        """
        deleted = 0
        for mem in list(self.list_all(limit=10_000)):
            if self.delete(str(mem.id)):
                deleted += 1
        return deleted

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

    def delete_by_filters(
        self, filters: tuple[tuple[str, Any], ...]
    ) -> int:
        """Delete all memories whose ``context.<field>`` matches ALL
        ``(field, value)`` pairs in ``filters``.

        Default fallback: narrow to the rarest-filter survivors via
        ``list_all``, then ``delete`` per row. This is O(rows) in the
        worst case and intended only for in-memory / single-process
        stores where the result set is naturally small.

        Backends with native composite-query support (SQLite,
        PostgreSQL, Redis, Neo4j, ChromaDB) MUST override this with a
        single multi-predicate query so the operation stays O(matches)
        instead of degrading to O(rows).

        All keys must be a dotted path inside ``Memory.context``.
        Returns the count of memories deleted (0 if no matches).

        Added in v0.6.0 to support multi-tenant GDPR deletion
        (``delete_by_project_id(project_id, tenant_id=...)``) without
        the previous O(N) list_all round-trip.
        """
        if not filters:
            return 0
        if len(filters) == 1:
            field, value = filters[0]
            return self.delete_by_filter(field, value)
        # Multi-predicate fallback. Only used by stores that have not
        # yet overridden this method (InMemoryStore + non-overridden
        # future subclasses). For these, list_all IS the data set.
        survivors: list[Memory] = list(self.list_all(limit=10_000))
        for field, value in filters:
            survivors = [
                m for m in survivors
                if getattr(m.context, field, None) == value
            ]
        if not survivors:
            return 0
        deleted = 0
        for mem in survivors:
            if self.delete(str(mem.id)):
                deleted += 1
        return deleted

    # ------------------------------------------------------------------
    # v0.6.x additions — used by CascadeForgetter to avoid
    # per-tier retrieve() sweeps (P1-5) and per-tier list_all() scans
    # for in-edge discovery (P0-3). Stores that don't yet implement
    # these should leave the abstract at the default and accept the
    # O(N) fallback; stores that do implement them get O(1) per
    # memory-id lookup / O(1) per in-edge query.
    # ------------------------------------------------------------------

    def find_tier(self, memory_id: str) -> bool:
        """Return True if this store contains ``memory_id``.

        v0.6.x: replaces the v0.5.x ``CascadeForgetter._locate_tier``
        pattern of ``for tier, store in self._stores.items():
        store.retrieve(memory_id)`` which issued N round-trips per
        cascade call. Default fallback is a single ``retrieve()``
        round-trip; backends with a primary-key index (SQLite,
        InMemory dict) get O(1) at the storage layer.

        Exceptions from ``retrieve`` bubble up so the cascade
        engine can log them at ERROR level and treat them as
        not-found without silently misclassifying a backend
        failure as "memory doesn't exist".
        """
        return self.retrieve(memory_id) is not None

    def in_edges(self, target_id: str) -> list[str]:
        """Return the list of memory_ids that reference ``target_id``.

        v0.6.x: replaces the v0.5.x ``_discover_in_edges`` ``scan``
        mode that walked ``list_all(limit=999999)`` on every cascade
        call.

        The default returns ``[]`` — a store without a maintained
        reverse index has no fast in-edge path. Cascade's ``scan``
        and ``auto`` modes fall back to a list_all scan in that
        case (see :py:meth:`in_edges_scan`). Stores that maintain
        a reverse index should override this with an O(1) lookup
        and override :py:attr:`has_reverse_index` to return True.

        Returned ids are NOT ordered and may include duplicates
        when multiple relations point at the same target — the
        cascade engine deduplicates via its visit_set.
        """
        return []

    has_reverse_index: bool = False

    def in_edges_scan(self, target_id: str) -> list[str]:
        """O(N) fallback: list_all + filter on
        ``metadata.relations[].target_memory_id``.

        v0.6.x: extracted from the inline _scan_in_edges_for_store
        helper in cascade.py. The ``scan`` and ``auto`` cascade
        modes call this on stores that don't override
        :py:meth:`in_edges`.
        """
        try:
            results: list[str] = []
            for mem in self.list_all(limit=10_000):
                for rel in mem.metadata.relations:
                    if rel.target_memory_id == target_id:
                        results.append(str(mem.id))
            return results
        except Exception:
            logger.exception(
                "in_edges_scan(%s) failed; returning empty", target_id,
            )
            return []
