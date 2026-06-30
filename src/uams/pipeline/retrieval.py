"""Retrieval pipeline: hybrid search with RRF fusion and token budget compression."""

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional

from uams.core.enums import MemoryType
from uams.core.models import AgentContext, Memory
from uams.storage.base import MemoryStore


class RetrievalPipeline:
    """
    Triple-stream retrieval combining three signals:
    1. BM25 / keyword matching
    2. Vector similarity (if embeddings available)
    3. Graph traversal (if entity relations exist)

    Fused with Reciprocal Rank Fusion (RRF) and session-diversified.
    """

    def __init__(
        self,
        stores: Dict[MemoryType, MemoryStore],
        rrf_k: int = 60,
        token_estimator: Optional[Any] = None,
    ):
        self._stores = stores
        self._rrf_k = rrf_k
        self._token_estimator = token_estimator

    def retrieve(
        self,
        query: str,
        context: AgentContext,
        vector: Optional[List[float]] = None,
        budget_tokens: int = 2000,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Memory]:
        """
        Universal retrieval pipeline.

        Steps:
        1. Working-tier exact match (recent events)
        2. Keyword search across Episodic + Semantic + Procedural
        3. Vector similarity (if embedding provided)
        4. Graph traversal (if entities in query)
        5. RRF fusion + recency/importance boosting
        6. Session diversification (max 3 per session)
        7. Token budget compression (greedy by importance)
        """
        all_results: Dict[str, List[tuple]] = defaultdict(list)

        # 1. Working tier (hot cache)
        working_results = self._stores[MemoryType.WORKING].search_keywords(query, k=10)
        for rank, mem in enumerate(working_results):
            all_results[str(mem.id)].append((mem, rank, "working"))

        # 2. BM25 across long-term tiers
        for tier in (MemoryType.EPISODIC, MemoryType.SEMANTIC, MemoryType.PROCEDURAL):
            if tier in self._stores:
                results = self._stores[tier].search_keywords(query, k=10)
                for rank, mem in enumerate(results):
                    all_results[str(mem.id)].append((mem, rank, "bm25"))

        # 3. Vector similarity
        if vector is not None:
            for tier in self._stores:
                results = self._stores[tier].search_vector(vector, k=10)
                for rank, mem in enumerate(results):
                    all_results[str(mem.id)].append((mem, rank, "vector"))

        # 4. Graph: treat query words as entities (limit to first 3 non-empty tokens to avoid explosion)
        entities = [w for w in query.split() if w.strip()][:3]
        for entity in entities:
            for tier in self._stores:
                results = self._stores[tier].search_graph(entity, depth=2)
                for rank, mem in enumerate(results):
                    all_results[str(mem.id)].append((mem, rank, "graph"))

        # 5. RRF fusion + boosting
        rrf_scores: Dict[str, float] = defaultdict(float)
        memory_map: Dict[str, Memory] = {}

        for mid, rankings in all_results.items():
            mem = rankings[0][0]
            memory_map[mid] = mem
            for _, rank, source in rankings:
                recency_boost = math.exp(-mem.anchor.age_seconds() / 3600 / 48)
                importance_boost = mem.metadata.importance / 10.0
                score = 1.0 / (self._rrf_k + rank + 1)
                score *= (1.0 + recency_boost + importance_boost)
                rrf_scores[mid] += score

        # 6. Diversify: max 3 per session to avoid collapse
        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
        final: List[Memory] = []
        session_counts: Dict[str, int] = defaultdict(int)

        for mid in sorted_ids:
            mem = memory_map[mid]
            sid = mem.context.session_id
            if session_counts[sid] >= 3:
                continue
            session_counts[sid] += 1
            mem.retrieval_score = rrf_scores[mid]
            mem.touch()
            final.append(mem)

        # 7. Token budget compression (greedy by importance)
        return self._compress_to_budget(final, budget_tokens)

    def _compress_to_budget(self, memories: List[Memory], budget: int) -> List[Memory]:
        """Greedy packing by importance, respecting token budget."""
        from uams.utils.tokens import estimate_tokens

        result: List[Memory] = []
        used = 0
        estimator = self._token_estimator
        estimate_fn = estimator.estimate if estimator else estimate_tokens

        # Sort by importance descending
        for mem in sorted(memories, key=lambda m: m.metadata.importance, reverse=True):
            tokens = estimate_fn(mem.payload.raw)
            if used + tokens > budget:
                break
            result.append(mem)
            used += tokens
        return result
