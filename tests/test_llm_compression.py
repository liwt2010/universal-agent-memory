"""Tests for LLMCompressionEngine and LLM client implementations.

All tests use mock LLM clients — no real API calls, no token spend.
"""

import json
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
from uams.config import UAMSConfig
from uams.llm.client import (
    CachedLLMClient,
    LLMClient,
    NullLLMClient,
)
from uams.pipeline.llm_compression import LLMCompressionEngine


class FakeLLMClient(LLMClient):
    """Records calls and returns scripted responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = []

    def chat(self, messages, **kwargs):
        self._calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        if not self._responses:
            raise RuntimeError("FakeLLMClient: no scripted response left")
        return self._responses.pop(0)


def _make_event(content, agent_id="a1", session_id="s1", ts=1.0):
    return AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=AgentContext(agent_id=agent_id, agent_type="t", session_id=session_id),
        content=content,
        timestamp=ts,
    )


def _make_episodic_memory(raw="narrative", agent_id="a1", session_id="s1"):
    return Memory(
        id=MemoryId(),
        anchor=TemporalAnchor(),
        context=AgentContext(agent_id=agent_id, agent_type="t", session_id=session_id),
        payload=MemoryPayload(raw=raw),
        metadata=MemoryMetadata(
            memory_type=MemoryType.EPISODIC,
            privacy=PrivacyLevel.INTERNAL,
        ),
    )


class TestLLMClientCache(unittest.TestCase):
    def test_cache_returns_same_result(self):
        inner = FakeLLMClient(["cached-response"])
        cached = CachedLLMClient(inner, max_entries=10)
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(cached.chat(msgs), "cached-response")
        self.assertEqual(cached.chat(msgs), "cached-response")
        self.assertEqual(len(inner._calls), 1)

    def test_cache_different_kwargs_no_hit(self):
        inner = FakeLLMClient(["a", "b"])
        cached = CachedLLMClient(inner, max_entries=10)
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(cached.chat(msgs, max_tokens=100), "a")
        self.assertEqual(cached.chat(msgs, max_tokens=200), "b")
        self.assertEqual(len(inner._calls), 2)

    def test_cache_evicts_when_full(self):
        inner = FakeLLMClient(["a", "b", "c", "d"])
        cached = CachedLLMClient(inner, max_entries=2)
        for content in ["m1", "m2", "m3"]:
            cached.chat([{"role": "user", "content": content}])
        # m3 should have evicted one older entry -> all 3 inner calls were made
        self.assertEqual(len(inner._calls), 3)

    def test_ttl_none_means_infinite(self):
        """Backward compat: ttl_seconds=None keeps the old 'cache forever' behavior."""
        inner = FakeLLMClient(["hello"])
        cached = CachedLLMClient(inner, ttl_seconds=None)
        cached.chat([{"role": "user", "content": "x"}])
        cached.chat([{"role": "user", "content": "x"}])
        cached.chat([{"role": "user", "content": "x"}])
        self.assertEqual(len(inner._calls), 1)

    def test_ttl_expired_evicts_stale_response(self):
        """After TTL elapses, the cached value is treated as absent so the inner
        client is invoked again. This prevents a hard-pin on stale model output
        (e.g. an episodic summary that no longer reflects the user's current
        preferences) — without it we'd be silently serving fake data.
        """
        inner = FakeLLMClient(["first", "second"])
        now = [100.0]
        cached = CachedLLMClient(inner, ttl_seconds=10.0, clock=lambda: now[0])
        msg = [{"role": "user", "content": "x"}]
        self.assertEqual(cached.chat(msg), "first")
        # 7 seconds later — within TTL — second call should hit cache.
        now[0] = 107.0
        self.assertEqual(cached.chat(msg), "first")
        # 11 seconds later — past TTL — must recompute.
        now[0] = 111.0
        self.assertEqual(cached.chat(msg), "second")
        self.assertEqual(len(inner._calls), 2)

    def test_ttl_negative_or_zero_treated_as_none(self):
        """Defensive: ttl<=0 should disable expiry, not poison every entry."""
        inner = FakeLLMClient(["hello"])
        cached = CachedLLMClient(inner, ttl_seconds=0.0)
        cached.chat([{"role": "user", "content": "x"}])
        cached.chat([{"role": "user", "content": "x"}])
        self.assertEqual(len(inner._calls), 1)

    def test_ttl_external_backend_uses_envelope(self):
        """Even when routed through external cache_get/cache_put, the client
        writes a TTL envelope so a downstream process with a different clock
        still respects the configured TTL.
        """
        store: dict = {}
        inner = FakeLLMClient(["a", "b"])
        cached = CachedLLMClient(
            inner,
            ttl_seconds=10.0,
            cache_get=store.get,
            cache_put=store.__setitem__,
            clock=lambda: 100.0,
        )
        msg = [{"role": "user", "content": "x"}]
        # Seed envelope at t=100 (expires at 110).
        cached.chat(msg)
        # Fresh client instance reads via same external backend but with
        # clock far past expiry.
        inner2 = FakeLLMClient(["b"])
        cached2 = CachedLLMClient(
            inner2,
            ttl_seconds=10.0,
            cache_get=store.get,
            cache_put=store.__setitem__,
            clock=lambda: 200.0,  # well past 110
        )
        cached2.chat(msg)
        self.assertEqual(len(inner2._calls), 1)  # recompute fired


class TestNullLLMClient(unittest.TestCase):
    def test_chat_raises(self):
        with self.assertRaises(RuntimeError):
            NullLLMClient().chat([{"role": "user", "content": "x"}])


class TestLLMCompressionEngine(unittest.TestCase):
    def _engine(self, client, **kwargs):
        return LLMCompressionEngine(client, max_events_per_call=5, timeout=5.0, **kwargs)

    def test_compress_episodic_calls_llm(self):
        client = FakeLLMClient(["Alice is vegetarian and likes boutique hotels."])
        engine = self._engine(client)
        events = [
            _make_event("I'm vegetarian", ts=1.0),
            _make_event("I prefer boutique hotels", ts=2.0),
        ]
        mem = engine.compress_working_to_episodic(events)
        self.assertEqual(
            mem.payload.raw,
            "Alice is vegetarian and likes boutique hotels.",
        )
        self.assertEqual(mem.metadata.memory_type, MemoryType.EPISODIC)
        self.assertEqual(len(client._calls), 1)
        # System + user
        self.assertEqual(client._calls[0]["messages"][0]["role"], "system")
        self.assertEqual(client._calls[0]["messages"][1]["role"], "user")

    def test_compress_fallback_on_llm_error(self):
        client = FakeLLMClient([])  # raises immediately (no scripted response)
        engine = self._engine(client)
        events = [_make_event("hello", ts=1.0)]
        mem = engine.compress_working_to_episodic(events)
        # Falls back to raw concatenation
        self.assertIn("[USER_INPUT] hello", mem.payload.raw)

    def test_compress_batches_large_event_lists(self):
        # 12 events > max_events_per_call=5 -> triggers two-level summarization
        client = FakeLLMClient(
            [
                "chunk-1-summary",
                "chunk-2-summary",
                "chunk-3-summary",
                "final-summary",  # meta-summary of the 3 chunk summaries
            ]
        )
        engine = self._engine(client)
        events = [_make_event(f"event-{i}", ts=float(i)) for i in range(12)]
        mem = engine.compress_working_to_episodic(events)
        self.assertEqual(mem.payload.raw, "final-summary")
        self.assertEqual(len(client._calls), 4)  # 3 chunks + 1 final

    def test_extract_semantic_parses_json(self):
        client = FakeLLMClient(
            [
                json.dumps(
                    [
                        {"key": "diet", "value": "vegetarian"},
                        {"key": "hotel_pref", "value": "boutique"},
                    ]
                )
            ]
        )
        engine = self._engine(client)
        episodic = _make_episodic_memory(raw="Alice is vegetarian and likes boutique hotels.")
        facts = engine.extract_semantic(episodic)
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0].payload.raw, "diet = vegetarian")
        self.assertEqual(facts[1].payload.raw, "hotel_pref = boutique")
        self.assertEqual(facts[0].metadata.memory_type, MemoryType.SEMANTIC)

    def test_extract_semantic_handles_code_fence(self):
        client = FakeLLMClient(['```json\n[{"key": "k", "value": "v"}]\n```'])
        engine = self._engine(client)
        facts = engine.extract_semantic(_make_episodic_memory())
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].payload.raw, "k = v")

    def test_extract_semantic_skips_invalid_items(self):
        client = FakeLLMClient(
            [
                json.dumps(
                    [
                        {"key": "good", "value": "yes"},
                        {"key": "", "value": "missing key"},  # skip
                        {"key": "noval", "value": ""},  # skip
                        "not-a-dict",  # skip
                    ]
                )
            ]
        )
        engine = self._engine(client)
        facts = engine.extract_semantic(_make_episodic_memory())
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].payload.raw, "good = yes")

    def test_extract_semantic_returns_empty_on_error(self):
        client = FakeLLMClient([])
        engine = self._engine(client)
        facts = engine.extract_semantic(_make_episodic_memory())
        self.assertEqual(facts, [])

    def test_extract_procedural_requires_two_episodes(self):
        client = FakeLLMClient([])
        engine = self._engine(client)
        self.assertEqual(engine.extract_procedural([]), [])
        self.assertEqual(engine.extract_procedural([_make_episodic_memory()]), [])

    def test_extract_procedural_filters_low_frequency(self):
        client = FakeLLMClient(
            [
                json.dumps(
                    [
                        {"pattern": "p1", "description": "d", "frequency": 3},
                        {"pattern": "p2", "description": "d", "frequency": 1},  # skip (<2)
                    ]
                )
            ]
        )
        engine = self._engine(client)
        episodes = [_make_episodic_memory(raw=f"ep {i}") for i in range(2)]
        procs = engine.extract_procedural(episodes)
        self.assertEqual(len(procs), 1)
        self.assertIn("p1", procs[0].payload.raw)
        self.assertEqual(procs[0].metadata.memory_type, MemoryType.PROCEDURAL)

    def test_extract_procedural_returns_empty_on_error(self):
        client = FakeLLMClient([])
        engine = self._engine(client)
        episodes = [_make_episodic_memory() for _ in range(2)]
        self.assertEqual(engine.extract_procedural(episodes), [])


class TestUAMSConfigLLMFields(unittest.TestCase):
    def test_default_llm_disabled(self):
        cfg = UAMSConfig()
        self.assertFalse(cfg.llm_enabled)
        self.assertIsNone(cfg.llm_api_key)

    def test_default_target_ratio(self):
        cfg = UAMSConfig()
        self.assertGreater(cfg.llm_compression_target_ratio, 0.0)
        self.assertLessEqual(cfg.llm_compression_target_ratio, 1.0)

    def test_llm_enabled_requires_api_key(self):
        with self.assertRaises(ValueError):
            UAMSConfig(llm_enabled=True, llm_api_key=None).validate()

    def test_llm_enabled_with_api_key_passes(self):
        cfg = UAMSConfig(llm_enabled=True, llm_api_key="dummy")
        cfg.validate()  # should not raise

    def test_invalid_provider(self):
        with self.assertRaises(ValueError):
            UAMSConfig(
                llm_enabled=True,
                llm_provider="anthropic",
                llm_api_key="dummy",
            ).validate()

    def test_invalid_timeout(self):
        with self.assertRaises(ValueError):
            UAMSConfig(llm_timeout_seconds=0.5).validate()
        with self.assertRaises(ValueError):
            UAMSConfig(llm_timeout_seconds=500).validate()

    def test_invalid_target_ratio(self):
        with self.assertRaises(ValueError):
            UAMSConfig(llm_compression_target_ratio=0.0).validate()
        with self.assertRaises(ValueError):
            UAMSConfig(llm_compression_target_ratio=1.5).validate()

    def test_invalid_temperature(self):
        with self.assertRaises(ValueError):
            UAMSConfig(llm_temperature=3.0).validate()


if __name__ == "__main__":
    unittest.main()