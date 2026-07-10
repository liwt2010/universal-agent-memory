"""In-memory reference implementation of MemoryStore with thread safety and capacity limits.

Uses RLock to protect all shared mutable state.
Supports LRU eviction when capacity is exceeded.
Suitable for testing and single-process deployments.

Production: swap for SQLiteStore, ChromaDBStore, etc.
"""

import math
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
        Real cosine similarity over memory embeddings.

        Edge cases — all return [] (or skip the offending memory, never raise):
          - empty query vector            -> []
          - zero-norm query vector        -> [] (cosine undefined)
          - memory has no embedding       -> skipped
          - dimension mismatch vs query   -> skipped
          - metadata filter (kwargs)      -> equality on metadata attributes

        Results sorted by cosine descending; ties broken by recency
        (already LRU position). On hits, ``mem.touch()`` is invoked and
        the entry is moved to the end of the LRU OrderedDict.
        """
        if not vector:
            return []
        # Zero-norm guard: every component is zero -> cosine is undefined
        q_sq = sum(float(x) * float(x) for x in vector)
        if q_sq == 0.0:
            return []

        # Snapshot memories + embeddings under lock, then sort outside the
        # lock (O(N log N) Python sort vs. holding the RLock — same pattern
        # as search_keywords keeps locks short).
        scored: List[Tuple[float, Memory]] = []
        with self._lock:
            snapshot = list(self._memories.items())
            query_dim = len(vector)
            for mid, mem in snapshot:
                emb = mem.payload.embedding if mem.payload else None
                if not emb or len(emb) != query_dim:
                    continue
                score = self._cosine_or_none(vector, emb, q_sq)
                # None means the memory embedding has zero norm (no
                # information content); orthogonal vectors legitimately
                # score 0.0 and stay in the result.
                if score is None:
                    continue
                if not self._metadata_matches(mem, filters):
                    continue
                scored.append((score, mem))

        # Stable sort by score desc; if equal, by created_at desc (tiebreaker)
        scored.sort(key=lambda pair: (pair[0], pair[1].anchor.created_at), reverse=True)

        results: List[Memory] = []
        with self._lock:
            for _score, mem in scored[:k]:
                if mem.payload is None:
                    continue
                mid = str(mem.id)
                if mid in self._memories:
                    mem.touch()
                    self._memories.move_to_end(mid)
                    results.append(mem)
        return results

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """Cosine similarity in [-1, 1].

        Returns 0.0 for: dimension mismatch, empty inputs, or zero-norm
        vectors (cosine is undefined when either side has zero length).
        Note: this collapses the orthogonal case (cos = 0) and the
        zero-norm case (cos = 0/0) into the same return value; if you
        need to distinguish them, use ``_cosine_or_none`` instead.
        """
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        a_sq = 0.0
        b_sq = 0.0
        for x, y in zip(a, b):
            fx = float(x)
            fy = float(y)
            dot += fx * fy
            a_sq += fx * fx
            b_sq += fy * fy
        if a_sq == 0.0 or b_sq == 0.0:
            return 0.0
        return dot / (math.sqrt(a_sq) * math.sqrt(b_sq))

    @staticmethod
    def _cosine_or_none(
        a: List[float], b: List[float], a_sq: float
    ) -> Optional[float]:
        """Like ``_cosine_similarity`` but returns ``None`` when ``b`` has
        zero norm (cosine undefined). The caller precomputes ``a_sq``
        (norm-squared of the query, known to be > 0 by the caller) to
        avoid recomputing for every memory in the loop.

        Disambiguates the two zero cases:
          - None            -> b is zero-norm (skip, no information)
          - 0.0 (a number)  -> cosine is genuinely zero (orthogonal)
        """
        if not a or not b or len(a) != len(b) or a_sq <= 0.0:
            return None
        dot = 0.0
        b_sq = 0.0
        for x, y in zip(a, b):
            dot += float(x) * float(y)
            b_sq += float(y) * float(y)
        if b_sq == 0.0:
            return None
        return dot / (math.sqrt(a_sq) * math.sqrt(b_sq))

    @staticmethod
    def _metadata_matches(mem: Memory, filters: Dict[str, Any]) -> bool:
        """Equality check on metadata attributes exposed to vector search.

        Currently supported filter keys:
          - memory_type  (MemoryType enum or its .name string)
          - privacy      (PrivacyLevel enum or its .name string)
        Unknown filter keys are ignored (no filter applied for them).
        """
        if not filters:
            return True
        meta = mem.metadata
        if "memory_type" in filters:
            want = filters["memory_type"]
            got = meta.memory_type.name if hasattr(meta.memory_type, "name") else meta.memory_type
            if isinstance(want, str) and got != want:
                return False
            if not isinstance(want, str) and want is not meta.memory_type:
                return False
        if "privacy" in filters:
            want = filters["privacy"]
            got = meta.privacy.name if hasattr(meta.privacy, "name") else meta.privacy
            if isinstance(want, str) and got != want:
                return False
            if not isinstance(want, str) and want is not meta.privacy:
                return False
        return True

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
