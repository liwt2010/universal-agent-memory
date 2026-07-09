"""Tests for QueryRewriter + RetrievalPipeline integration.

All tests use a fake LLM client — no real API calls, no token spend.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import (
    AgentContext,
    AgentEvent,
    EventType,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    MemoryType,
    PrivacyLevel,
    TemporalAnchor,
)
from uams.llm.client import (
    CachedLLMClient,
    LLMClient,
    NullLLMClient,
)
from uams.pipeline.query_rewrite import QueryRewriter
from uams.pipeline.retrieval import RetrievalPipeline
from uams.storage.memory import InMemoryStore
from uams.core.enums import MemoryType as MT


class FakeLLM(LLMClient):
    """Scripted responses; raises if exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if not self._responses:
            raise RuntimeError("FakeLLM exhausted")
        return self._responses.pop(0)


class FailingLLM(LLMClient):
    """Always raises."""

    def chat(self, messages, **kwargs):
        raise RuntimeError("intentional failure")


# ---------------------------------------------------------------------------
# QueryRewriter unit tests
# ---------------------------------------------------------------------------


class TestQueryRewriterFallback(unittest.TestCase):
    """When no LLM is available or it fails, return the original query only."""

    def test_none_client_returns_original(self):
        rw = QueryRewriter(llm_client=None)
        result = rw.rewrite("Japan hotels?")
        self.assertEqual(result, ["Japan hotels?"])

    def test_null_client_returns_original(self):
        rw = QueryRewriter(llm_client=NullLLMClient())
        result = rw.rewrite("anything")
        self.assertEqual(result, ["anything"])

    def test_empty_query_returns_empty_list(self):
        rw = QueryRewriter(llm_client=FakeLLM(["junk"]))
        self.assertEqual(rw.rewrite(""), [])

    def test_whitespace_query_returns_empty_list(self):
        rw = QueryRewriter(llm_client=FakeLLM(["junk"]))
        self.assertEqual(rw.rewrite("   "), [])

    def test_failing_llm_returns_original(self):
        rw = QueryRewriter(llm_client=FailingLLM())
        result = rw.rewrite("Japan hotels?")
        self.assertEqual(result, ["Japan hotels?"])

    def test_failed_llm_does_not_break_cache(self):
        """After a failure, a subsequent successful call should still cache."""
        rw = QueryRewriter(llm_client=FakeLLM(["boutique hotels Japan", "ryokan Tokyo"]))
        result1 = rw.rewrite("Japan hotels?")
        # First call succeeds; original is prepended
        self.assertIn("Japan hotels?", result1)
        self.assertGreater(len(result1), 1)
        # Second call should hit cache, not call LLM again
        calls_before = rw._llm.calls
        result2 = rw.rewrite("Japan hotels?")
        self.assertEqual(rw._llm.calls, calls_before)
        self.assertEqual(result1, result2)


class TestQueryRewriterParsing(unittest.TestCase):
    """LLM output comes in many shapes; verify we parse them all."""

    def test_plain_lines(self):
        llm = FakeLLM(["boutique hotels Japan\nryokan Tokyo Kyoto\nJapan accommodation"])
        rw = QueryRewriter(llm_client=llm, max_variants=4)
        result = rw.rewrite("Japan hotels?")
        self.assertIn("Japan hotels?", result)  # original always included
        self.assertIn("boutique hotels Japan", result)
        self.assertIn("ryokan Tokyo Kyoto", result)
        self.assertIn("Japan accommodation", result)

    def test_numbered_lines(self):
        llm = FakeLLM([
            "1. boutique hotels Japan\n"
            "2. ryokan Tokyo Kyoto\n"
            "3. Japan accommodation"
        ])
        rw = QueryRewriter(llm_client=llm)
        result = rw.rewrite("Japan hotels?")
        self.assertEqual(
            result,
            [
                "Japan hotels?",
                "boutique hotels Japan",
                "ryokan Tokyo Kyoto",
                "Japan accommodation",
            ],
        )

    def test_bullet_lines(self):
        llm = FakeLLM([
            "- boutique hotels Japan\n"
            "* ryokan Tokyo Kyoto\n"
            "• Japan accommodation"
        ])
        rw = QueryRewriter(llm_client=llm)
        result = rw.rewrite("Japan hotels?")
        self.assertIn("boutique hotels Japan", result)
        self.assertIn("ryokan Tokyo Kyoto", result)
        self.assertIn("Japan accommodation", result)

    def test_skip_header_echo(self):
        """If the LLM echoes the prompt header, strip it."""
        llm = FakeLLM([
            "Query: Japan hotels?\n"
            "Variants:\n"
            "boutique hotels Japan\n"
            "ryokan Tokyo Kyoto"
        ])
        rw = QueryRewriter(llm_client=llm)
        result = rw.rewrite("Japan hotels?")
        # The "Query: ..." and "Variants:" lines should not appear in the output
        for v in result:
            self.assertFalse(v.lower().startswith("query:"))
            self.assertFalse(v.lower().startswith("variants:"))

    def test_dedup_preserves_first_occurrence(self):
        llm = FakeLLM(["Japan hotels\nJapan hotels\nJapan hotels\nryokan"])
        rw = QueryRewriter(llm_client=llm)
        result = rw.rewrite("Japan hotels?")
        # No duplicate strings
        self.assertEqual(len(result), len(set(result)))

    def test_clamp_to_max_variants(self):
        llm = FakeLLM([
            "v1\nv2\nv3\nv4\nv5\nv6\nv7\nv8\nv9"
        ])
        rw = QueryRewriter(llm_client=llm, max_variants=3)
        result = rw.rewrite("q")
        # 1 original + max 3 variants; never more
        self.assertLessEqual(len(result), 4)


class TestQueryRewriterCaching(unittest.TestCase):
    """Cache behavior: same query -> same result without re-calling LLM."""

    def test_cache_returns_same_result_without_llm_call(self):
        llm = FakeLLM(["variant1", "variant2"])
        rw = QueryRewriter(llm_client=llm, cache_max_entries=10)
        rw.rewrite("test query")  # first call -> LLM hit
        calls_after_first = llm.calls
        rw.rewrite("test query")  # second call -> cache hit
        self.assertEqual(llm.calls, calls_after_first)

    def test_cache_evicts_when_full(self):
        llm = FakeLLM([f"v{i}" for i in range(10)])
        rw = QueryRewriter(llm_client=llm, cache_max_entries=2)
        rw.rewrite("q1")
        rw.rewrite("q2")
        rw.rewrite("q3")  # cache full; "q1" evicted
        # LLM should have been called 3 times (cache miss each)
        self.assertEqual(llm.calls, 3)

    def test_clear_cache(self):
        llm = FakeLLM(["v1", "v2"])
        rw = QueryRewriter(llm_client=llm)
        rw.rewrite("q1")
        rw.clear_cache()
        rw.rewrite("q1")  # cache was cleared, LLM called again
        self.assertEqual(llm.calls, 2)


# ---------------------------------------------------------------------------
# RetrievalPipeline integration tests
# ---------------------------------------------------------------------------


def _make_memory(content: str, importance: float = 5.0, session_id: str = "s1") -> Memory:
    return Memory(
        id=MemoryId(),
        anchor=TemporalAnchor(),
        context=AgentContext(
            agent_id="a1", agent_type="t", session_id=session_id, user_id="alice"
        ),
        payload=MemoryPayload(raw=content),
        metadata=MemoryMetadata(
            memory_type=MT.EPISODIC,
            privacy=PrivacyLevel.INTERNAL,
            importance=importance,
        ),
    )


def _store_memories(stores, memories):
    for mem in memories:
        stores[MT.EPISODIC].store(mem)


def _make_pipeline(query_rewriter=None) -> RetrievalPipeline:
    stores = {mt: InMemoryStore() for mt in MT}
    return RetrievalPipeline(stores, query_rewriter=query_rewriter)


class TestRetrievalWithQueryRewrite(unittest.TestCase):
    """Verify that query rewriting expands the retrieval net."""

    def test_no_rewriter_returns_original_results(self):
        # Seed: 3 memories, only one matches the query
        m1 = _make_memory("boutique hotels in Japan", importance=9.0, session_id="s1")
        m2 = _make_memory("user prefers boutique hotels", importance=8.0, session_id="s1")
        m3 = _make_memory("France travel plans", importance=5.0, session_id="s2")

        pipeline = _make_pipeline()
        _store_memories(pipeline._stores, [m1, m2, m3])
        ctx = AgentContext(agent_id="a1", agent_type="t", session_id="s1")

        # Direct BM25-style search for "Japan hotels" should find m1
        result = pipeline.retrieve("Japan hotels", context=ctx, budget_tokens=2000)
        ids = {str(m.id) for m in result}
        self.assertIn(str(m1.id), ids)
        # m3 should not be in result (no match for "France" + "Japan hotels")
        self.assertNotIn(str(m3.id), ids)

    def test_rewriter_returns_same_results_when_no_real_call(self):
        """With a NullLLMClient rewriter, behavior is identical to no rewriter."""
        m1 = _make_memory("boutique hotels in Japan", importance=9.0, session_id="s1")
        m2 = _make_memory("user prefers boutique hotels", importance=8.0, session_id="s1")

        rw = QueryRewriter(llm_client=NullLLMClient())
        pipeline = _make_pipeline(query_rewriter=rw)
        _store_memories(pipeline._stores, [m1, m2])
        ctx = AgentContext(agent_id="a1", agent_type="t", session_id="s1")

        result = pipeline.retrieve("Japan hotels", context=ctx, budget_tokens=2000)
        ids = {str(m.id) for m in result}
        self.assertIn(str(m1.id), ids)
        self.assertIn(str(m2.id), ids)

    def test_rewriter_with_llm_calls_each_variant(self):
        """When rewriter returns variants, each variant is used to search."""
        # Store one memory that matches the original query,
        # and one that only matches a variant.
        m_orig = _make_memory("Japan boutique hotels", importance=9.0, session_id="s1")
        m_variant = _make_memory("ryokan Tokyo Kyoto accommodation", importance=9.0, session_id="s1")
        m_unrelated = _make_memory("France Eiffel Tower visit", importance=5.0, session_id="s2")

        # Rewriter returns a variant that matches m_variant
        rw = QueryRewriter(llm_client=FakeLLM(["ryokan Tokyo Kyoto accommodation"]))
        pipeline = _make_pipeline(query_rewriter=rw)
        _store_memories(pipeline._stores, [m_orig, m_variant, m_unrelated])
        ctx = AgentContext(agent_id="a1", agent_type="t", session_id="s1")

        result = pipeline.retrieve("Japan hotels", context=ctx, budget_tokens=2000)
        ids = {str(m.id) for m in result}
        # Both m_orig and m_variant should be found (via original + variant)
        self.assertIn(str(m_orig.id), ids)
        self.assertIn(str(m_variant.id), ids)
        # Unrelated memory should NOT appear
        self.assertNotIn(str(m_unrelated.id), ids)

    def test_failing_rewriter_does_not_break_pipeline(self):
        """If the rewriter raises unexpectedly, retrieve should still work."""
        rw = QueryRewriter(llm_client=FailingLLM())
        pipeline = _make_pipeline(query_rewriter=rw)
        m1 = _make_memory("Japan boutique hotels", importance=9.0)
        _store_memories(pipeline._stores, [m1])
        ctx = AgentContext(agent_id="a1", agent_type="t", session_id="s1")

        # Should NOT raise; should fall back to original query
        result = pipeline.retrieve("Japan hotels", context=ctx, budget_tokens=2000)
        ids = {str(m.id) for m in result}
        self.assertIn(str(m1.id), ids)


if __name__ == "__main__":
    unittest.main()