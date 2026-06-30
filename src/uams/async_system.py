"""Async wrappers for UAMS.

Provides async/await versions of the core API for non-blocking I/O.
Uses asyncio.Lock for concurrency safety instead of threading.RLock.
"""

import asyncio
from typing import Dict, List, Optional

from uams.system import UniversalMemorySystem
from uams.core.models import AgentContext, AgentEvent, Memory, MemoryId
from uams.multi_agent.coordinator import Signal
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class AsyncUniversalMemorySystem:
    """
    Async wrapper around UniversalMemorySystem.
    All public methods are async and use asyncio.Lock for safety.
    """

    def __init__(self, ums: Optional[UniversalMemorySystem] = None):
        self._ums = ums or UniversalMemorySystem()
        self._lock = asyncio.Lock()

    async def observe(self, event: AgentEvent) -> None:
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(None, self._ums.observe, event)

    async def remember(
        self,
        fact: str,
        context: AgentContext,
        importance: float = 5.0,
        category: str = "general",
        privacy = None,
        tags: Optional[set] = None,
    ) -> Optional[MemoryId]:
        from uams.core.enums import PrivacyLevel
        privacy = privacy or PrivacyLevel.PUBLIC
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.remember, fact, context, importance, category, privacy, tags
            )

    async def recall(
        self,
        query: str,
        context: AgentContext,
        budget_tokens: int = None,
    ) -> List[Memory]:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.recall, query, context, budget_tokens
            )

    async def forget(self, memory_id: str) -> bool:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.forget, memory_id
            )

    async def inject_context(
        self,
        query: str,
        context: AgentContext,
        budget_tokens: int = None,
    ) -> str:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.inject_context, query, context, budget_tokens
            )

    async def decay_sweep(self) -> int:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.decay_sweep
            )

    async def acquire_lock(self, agent_id: str, resource: str, ttl: float = 300.0) -> bool:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.acquire_lock, agent_id, resource, ttl
            )

    async def release_lock(self, agent_id: str, resource: str) -> bool:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.release_lock, agent_id, resource
            )

    async def send_signal(self, signal: Signal) -> None:
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None, self._ums.send_signal, signal
            )

    async def read_signals(self, agent_id: str) -> List[Signal]:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.read_signals, agent_id
            )

    async def get_stats(self) -> Dict[str, int]:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._ums.get_stats
            )

    async def clear(self) -> None:
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(None, self._ums.clear)
