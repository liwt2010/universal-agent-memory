"""Tests for RetrievalPipeline budget compression logic.

Specifically targets ``_compress_to_budget`` which packs memories into a
token budget using a relevance-density (``score / tokens``) ranking instead
of pure importance.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import (
    AgentContext,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    MemoryType,
    PrivacyLevel,
    TemporalAnchor,
)
from uams.pipeline.retrieval import RetrievalPipeline
from uams.storage.memory import InMemoryStore
from uams.core.enums import MemoryType as MT


def _make_memory(content: str, importance: float = 5.0, score: float = None) -> Memory:
    """Build a Memory with explicit importance and optional retrieval_score."""
    mem = Memory(
        id=MemoryId(),
        anchor=TemporalAnchor(),
        context=AgentContext(agent_id="a1", agent_type="t", session_id="s1"),
        payload=MemoryPayload(raw=content),
        metadata=MemoryMetadata(
            memory_type=MT.EPISODIC,
            privacy=PrivacyLevel.INTERNAL,
            importance=importance,
        ),
    )
    if score is not None:
        mem.retrieval_score = score
    return mem


def _make_pipeline() -> RetrievalPipeline:
    """Build a minimal RetrievalPipeline for unit-testing the budget packer."""
    from uams.core.enums import MemoryType as MTEnum
    stores = {mt: InMemoryStore() for mt in MTEnum}
    return RetrievalPipeline(stores)


class TestRelevanceDensityPacking(unittest.TestCase):
    """Core behavior: prefer high-score short memories over long ones."""

    def test_short_medium_score_beats_long_high_score(self):
        """
        Scenario: budget = 50 tokens
        - mem_a: importance=10, content="x" * 200 (~50 tokens), score=0.1
        - mem_b: importance=5,  content="y" * 20  (~5 tokens),  score=0.05

        Pure importance: mem_a first → fills budget with one long low-score
        memory.
        Relevance-density: mem_a density = 0.1/50 = 0.002; mem_b density = 0.05/5
        = 0.01. mem_b ranks higher and fits, leaving room for more.
        """
        pipeline = _make_pipeline()
        mem_a = _make_memory("x" * 200, importance=10.0, score=0.1)
        mem_b = _make_memory("y" * 20, importance=5.0, score=0.05)
        result = pipeline._compress_to_budget([mem_a, mem_b], budget=50)
        # mem_b should be selected (better density)
        self.assertIn(mem_b, result)
        # mem_a may or may not fit depending on heuristic token estimate;
        # but at least one of the two short/medium-score memories was preferred.
        self.assertGreaterEqual(len(result), 1)

    def test_skip_long_memory_continue_with_shorter(self):
        """
        Scenario: 3 memories sorted by importance desc:
        - mem_1: importance=9, very long (won't fit alone in budget=20)
        - mem_2: importance=5, short (fits)
        - mem_3: importance=3, short (fits)

        Old code: break after mem_1 → empty result.
        New code: skip mem_1 (too long), continue with mem_2 + mem_3.
        """
        pipeline = _make_pipeline()
        mem_1 = _make_memory("a" * 500, importance=9.0)
        mem_2 = _make_memory("b" * 10, importance=5.0)
        mem_3 = _make_memory("c" * 10, importance=3.0)
        result = pipeline._compress_to_budget([mem_1, mem_2, mem_3], budget=20)
        # The key behavior: at least mem_2 or mem_3 should be in result,
        # not just an empty list.
        self.assertGreater(len(result), 0, "Skip-not-break should rescue at least one short memory")
        self.assertNotIn(mem_1, result, "Long over-budget memory should be skipped, not breaking")

    def test_falls_back_to_importance_when_no_retrieval_score(self):
        """When retrieval_score is not set, use importance as the score."""
        pipeline = _make_pipeline()
        m_high = _make_memory("d" * 5, importance=10.0)
        m_low = _make_memory("e" * 5, importance=2.0)
        result = pipeline._compress_to_budget([m_low, m_high], budget=100)
        # Both fit easily; high importance should come first in the (sparse) ordering
        self.assertEqual(len(result), 2)

    def test_empty_memories(self):
        pipeline = _make_pipeline()
        result = pipeline._compress_to_budget([], budget=100)
        self.assertEqual(result, [])

    def test_zero_tokens_memory_does_not_infinite_loop(self):
        """A 0-token memory would cause division by zero. We use max(1, tokens)."""
        pipeline = _make_pipeline()
        m_empty = _make_memory("", importance=5.0, score=1.0)
        # Should not raise (no ZeroDivisionError), should include the empty memory
        result = pipeline._compress_to_budget([m_empty], budget=100)
        self.assertEqual(len(result), 1)

    def test_all_oversized_memories(self):
        """If every memory exceeds budget, return empty."""
        pipeline = _make_pipeline()
        m1 = _make_memory("x" * 5000, importance=10.0)
        m2 = _make_memory("y" * 5000, importance=9.0)
        result = pipeline._compress_to_budget([m1, m2], budget=10)
        self.assertEqual(result, [])

    def test_budget_fits_all(self):
        """If budget > total tokens, include all."""
        pipeline = _make_pipeline()
        m1 = _make_memory("a" * 50, importance=5.0)
        m2 = _make_memory("b" * 50, importance=5.0)
        result = pipeline._compress_to_budget([m1, m2], budget=10000)
        self.assertEqual(len(result), 2)

    def test_retrieval_score_wins_over_importance(self):
        """
        When retrieval_score is set, it should drive the density calculation,
        not importance. A memory with low importance but high RRF score
        should rank above high-importance low-RRF-score memories of similar
        length.
        """
        pipeline = _make_pipeline()
        # Same length, but mem_a has high importance + low RRF; mem_b has low
        # importance + high RRF (e.g. very fresh and relevant).
        mem_a = _make_memory("x" * 30, importance=10.0, score=0.01)
        mem_b = _make_memory("y" * 30, importance=2.0, score=0.5)
        # Budget tight enough that only one fits
        result = pipeline._compress_to_budget([mem_a, mem_b], budget=12)
        # mem_b should win on density (score/token)
        self.assertEqual(len(result), 1)
        self.assertIn(mem_b, result)


class TestBackwardCompatWithImportanceOnly(unittest.TestCase):
    """The old API used pure importance; verify the new code still respects
    it when retrieval_score is absent."""

    def test_only_importance_sorting_when_no_scores(self):
        pipeline = _make_pipeline()
        m1 = _make_memory("a" * 10, importance=10.0)
        m2 = _make_memory("b" * 10, importance=5.0)
        m3 = _make_memory("c" * 10, importance=2.0)
        result = pipeline._compress_to_budget([m1, m2, m3], budget=1000)
        self.assertEqual(len(result), 3)
        # All fit, so order within doesn't matter for this test; just check all present
        self.assertCountEqual(result, [m1, m2, m3])


if __name__ == "__main__":
    unittest.main()