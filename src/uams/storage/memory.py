"""In-memory reference implementation of MemoryStore with thread safety and capacity limits.

Uses RLock to protect all shared mutable state.
Supports LRU eviction when capacity is exceeded.
Suitable for testing and single-process deployments.

Production: swap for SQLiteStore, ChromaDBStore, etc.
"""

import re
import threading
from collections import defaultdict, OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple

from uams.storage.base import MemoryStore
from uams.core.models import Memory
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class InMemoryStore(MemoryStore):
    """
    Thread-safe reference implementation using plain Python dicts and sets.
    Supports LRU eviction and capacity limits to prevent OOM.
    """

    def __init__(self, max_capacity: int = 10000):
        self._max_capacity = max_capacity
        self._memories: OrderedDict[str, Memory] = OrderedDict()
        self._keyword_index: Dict[str, Set[str]] = defaultdict(set)
        self._lock = threading.RLock()

    def store(self, memory: Memory) -> None:
        with self._lock:
            mid = str(memory.id)
            # LRU eviction if at capacity
            if mid not in self._memories and len(self._memories) >= self._max_capacity:
                evicted_mid, _ = self._memories.popitem(last=False)  # oldest
                # Remove from keyword index
                for token_set in self._keyword_index.values():
                    token_set.discard(evicted_mid)
                logger.debug("LRU evicted memory %s (capacity=%d)", evicted_mid, self._max_capacity)
            self._memories[mid] = memory
            self._memories.move_to_end(mid)  # mark as recently used
            doc = memory.payload.to_search_doc().lower()
            tokens = self._tokenize(doc)
            for token in tokens:
                self._keyword_index[token].add(mid)

    def retrieve(self, memory_id: str) -> Optional[Memory]:
        with self._lock:
            mem = self._memories.get(memory_id)
            if mem:
                mem.touch()
                self._memories.move_to_end(memory_id)
            return mem

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            if memory_id not in self._memories:
                return False
            del self._memories[memory_id]
            for token_set in self._keyword_index.values():
                token_set.discard(memory_id)
            return True

    def search_keywords(self, query: str, k: int = 10) -> List[Memory]:
        """Simple TF-like scoring: count matching tokens."""
        tokens = self._tokenize(query.lower())
        scores: Dict[str, int] = defaultdict(int)

        with self._lock:
            for token in tokens:
                for mid in self._keyword_index.get(token, set()):
                    scores[mid] += 1

            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
            results = []
            for mid, _ in ranked:
                mem = self._memories.get(mid)
                if mem:
                    mem.touch()
                    self._memories.move_to_end(mid)
                    results.append(mem)
            return results

    def search_vector(
        self, vector: List[float], k: int = 10, **filters: Any
    ) -> List[Memory]:
        """
        Fallback: return recent memories if no embedding available.
        Production implementations should compute cosine similarity.
        """
        with self._lock:
            all_mems = sorted(
                self._memories.values(),
                key=lambda m: m.anchor.created_at,
                reverse=True,
            )
            return all_mems[:k]

    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        """BFS over memory relations."""
        results: List[Memory] = []
        visited: Set[str] = set()
        queue: List[Tuple[str, int]] = [(entity, 0)]

        with self._lock:
            while queue:
                current, d = queue.pop(0)
                if d > depth or current in visited:
                    continue
                visited.add(current)

                for mem in self._memories.values():
                    mid = str(mem.id)
                    if mid == current:
                        results.append(mem)
                    for rel in mem.metadata.relations:
                        if rel.target_memory_id == current or mid == current:
                            if rel.target_memory_id not in visited:
                                queue.append((rel.target_memory_id, d + 1))

            return results

    def list_all(self, limit: int = 100) -> List[Memory]:
        with self._lock:
            return list(self._memories.values())[:limit]

    def delete_expired(self) -> int:
        with self._lock:
            expired = [mid for mid, mem in self._memories.items() if mem.anchor.is_expired()]
            for mid in expired:
                self.delete(mid)
            return len(expired)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Simple word tokenization supporting CJK."""
        return re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text)
