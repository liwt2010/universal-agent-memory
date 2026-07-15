"""Async wrappers for UAMS.

Provides async/await versions of the core API for non-blocking I/O.

Implementation note
==================

The underlying ``MemoryStore`` implementations are all synchronous
(pysqlite, psycopg2, redis-py, neo4j, chromadb). True async I/O would
require either:

  - re-implementing all 6 stores with their async equivalents
    (aiosqlite / asyncpg / redis.asyncio / neo4j-async-driver / httpx-
    backed chromadb), or
  - introducing an ``AsyncMemoryStore`` ABC that mirrors the existing
    ``MemoryStore`` shape and lets each backend ship both implementations.

Neither is in scope for the current release. Instead, this module
runs the sync calls on the default executor via ``asyncio.to_thread``
and gates them with per-method ``asyncio.Lock`` instances so two
concurrent ``observe`` calls don't race on ``_session_events`` while
two ``recall`` calls don't race on each other.

The trade-off: an event loop that issues many synchronous DB calls
will saturate the executor's default thread pool (8 workers on
Linux, scaled to ``min(32, os.cpu_count() + 4)`` on Python 3.8+).
Operators needing true async I/O should run ``UniversalMemorySystem``
in a dedicated thread pool and call it from async code, or migrate
their store backend to a native async driver.
"""

from __future__ import annotations

import asyncio

from uams.system import UniversalMemorySystem
from uams.core.models import AgentContext, AgentEvent, Memory, MemoryId
from uams.multi_agent.coordinator import Signal
from uams.pipeline.cascade import CascadeReport, CascadeStrategy
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class AsyncUniversalMemorySystem:
    """Async wrapper around ``UniversalMemorySystem``.

    Each public method runs the underlying sync implementation on the
    default executor. Per-method ``asyncio.Lock`` instances serialize
    operations that share mutable state — for example, two ``observe``
    calls both mutate ``_session_events``, so they share a lock; two
    ``recall`` calls share a different lock; ``observe`` and
    ``recall`` can run concurrently with each other.
    """

    def __init__(self, ums: Optional[UniversalMemorySystem] = None):
        self._ums = ums or UniversalMemorySystem()
        # Per-method locks so unrelated operations don't serialize
        # against each other. A single ``_lock`` on the whole class
        # would force every async call into a critical section, which
        # defeats the purpose of an async API.
        self._observe_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        self._coord_lock = asyncio.Lock()
        self._sweep_lock = asyncio.Lock()

    async def observe(self, event: AgentEvent) -> None:
        async with self._observe_lock:
            await asyncio.to_thread(self._ums.observe, event)

    async def remember(
        self,
        fact: str,
        context: AgentContext,
        importance: float = 5.0,
        category: str = "general",
        privacy=None,
        tags: set | None = None,
    ) -> MemoryId | None:
        from uams.core.enums import PrivacyLevel
        privacy = privacy or PrivacyLevel.PUBLIC
        async with self._store_lock:
            return await asyncio.to_thread(
                self._ums.remember, fact, context, importance, category, privacy, tags
            )

    async def recall(
        self,
        query: str,
        context: AgentContext,
        budget_tokens: int = None,
    ) -> list[Memory]:
        async with self._store_lock:
            return await asyncio.to_thread(
                self._ums.recall, query, context, budget_tokens
            )

    async def forget(
        self,
        memory_id: str,
        *,
        cascade: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
        max_depth: int | None = None,
        in_edge_mode: str | None = None,
    ) -> CascadeReport:
        """Forget a memory with configurable cascade.

        Mirrors the sync ``UniversalMemorySystem.forget()`` signature:
        returns a ``CascadeReport`` (NOT a bool). See
        ``docs/CASCADE_FORGET.md`` for the cascade strategy enum and
        GDPR-aligned workflow.
        """
        async with self._store_lock:
            return await asyncio.to_thread(
                lambda: self._ums.forget(
                    memory_id,
                    cascade=cascade,
                    max_depth=max_depth,
                    in_edge_mode=in_edge_mode,
                )
            )

    async def inject_context(
        self,
        query: str,
        context: AgentContext,
        budget_tokens: int = None,
    ) -> str:
        async with self._store_lock:
            return await asyncio.to_thread(
                self._ums.inject_context, query, context, budget_tokens
            )

    async def decay_sweep(self) -> int:
        async with self._sweep_lock:
            return await asyncio.to_thread(self._ums.decay_sweep)

    async def acquire_lock(
        self, agent_id: str, resource: str, ttl: float = 300.0
    ) -> bool:
        async with self._coord_lock:
            return await asyncio.to_thread(
                self._ums.acquire_lock, agent_id, resource, ttl
            )

    async def release_lock(self, agent_id: str, resource: str) -> bool:
        async with self._coord_lock:
            return await asyncio.to_thread(
                self._ums.release_lock, agent_id, resource
            )

    async def send_signal(self, signal: Signal) -> None:
        async with self._coord_lock:
            await asyncio.to_thread(self._ums.send_signal, signal)

    async def read_signals(self, agent_id: str) -> list[Signal]:
        async with self._coord_lock:
            return await asyncio.to_thread(self._ums.read_signals, agent_id)

    async def get_stats(self) -> dict[str, int]:
        async with self._store_lock:
            return await asyncio.to_thread(self._ums.get_stats)

    async def clear(self) -> None:
        async with self._store_lock:
            await asyncio.to_thread(self._ums.clear)
