"""Regression test for T08 (P1-4): retrieval_score=0.0 falsy bug.

Pins that a memory with `retrieval_score=0.0` is NOT silently
treated as "no score set" and routed to importance-based density.

Before the fix:
    score = getattr(mem, "retrieval_score", None) or mem.metadata.importance
    # 0.0 is falsy → falls back to importance
After:
    score = ...; if score is None: score = importance
    # 0.0 stays 0.0
"""

from __future__ import annotations

import unittest

from uams import (
    AgentContext,
    EventType,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    MemoryType,
    PrivacyLevel,
    TemporalAnchor,
    UniversalMemorySystem,
)
from uams.pipeline.retrieval import RetrievalPipeline


def _mk_memory(idx: int, raw: str, importance: float, retrieval_score: float | None) -> Memory:
    """Helper to build a synthetic Memory for density sort testing."""
    return Memory(
        id=MemoryId(f"m-{idx}"),
        anchor=TemporalAnchor(),
        context=AgentContext(agent_id="a", agent_type="t", session_id="s", user_id="u"),
        payload=MemoryPayload(raw=raw, structured={}, embedding=None),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
            importance=importance,
        ),
        retrieval_score=retrieval_score,
    )


class TestRetrievalScoreZero(unittest.TestCase):
    def test_zero_score_preserved_not_fallback_to_importance(self) -> None:
        """A Memory with retrieval_score=0.0 must sort by 0.0 density,
        not by importance=1.0 density.
        """
        u = UniversalMemorySystem()
        try:
            pipe = RetrievalPipeline(stores=[u._stores[MemoryType.SEMANTIC]])

            short_zero = _mk_memory(1, "short text", importance=10.0, retrieval_score=0.0)
            short_one = _mk_memory(2, "short text", importance=1.0, retrieval_score=1.0)

            # budget big enough to fit both
            result = pipe._compress_to_budget(
                [short_zero, short_one],
                budget=1000,
            )

            # density = score / tokens.
            # short_zero: 0.0 / 5 ≈ 0
            # short_one: 1.0 / 5 = 0.2
            # _compress_to_budget returns in DESC density order, so
            # short_one must come first.
            ids = [str(m.id) for m in result]
            self.assertEqual(ids, ["m-2", "m-1"])

        finally:
            u.shutdown()

    def test_none_score_still_falls_back_to_importance(self) -> None:
        """Backwards compat: a Memory with retrieval_score=None
        (legacy / unset) must continue to fall back to importance.
        """
        u = UniversalMemorySystem()
        try:
            pipe = RetrievalPipeline(stores=[u._stores[MemoryType.SEMANTIC]])

            short_none = _mk_memory(1, "short", importance=10.0, retrieval_score=None)
            short_one = _mk_memory(2, "short", importance=1.0, retrieval_score=1.0)

            result = pipe._compress_to_budget(
                [short_none, short_one],
                budget=1000,
            )
            # short_none density = 10/5 = 2.0; short_one = 1/5 = 0.2
            # So short_none must come first
            ids = [str(m.id) for m in result]
            self.assertEqual(ids, ["m-1", "m-2"])
        finally:
            u.shutdown()


if __name__ == "__main__":
    unittest.main()