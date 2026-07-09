"""Tests for HierarchicalFilter (L1 filter + L2 keyword extraction)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import AgentContext, AgentEvent, EventType
from uams.pipeline.hierarchical_filter import HierarchicalFilter


def _ev(content, event_type=EventType.USER_INPUT, structured=None, ts=1.0, agent_id="a1", session_id="s1"):
    return AgentEvent(
        event_type=event_type,
        agent_context=AgentContext(agent_id=agent_id, agent_type="t", session_id=session_id),
        content=content,
        structured_data=structured,
        timestamp=ts,
    )


class TestL1StructuralFilter(unittest.TestCase):
    """filter_events drops low-quality and duplicate events."""

    def test_empty_list(self):
        f = HierarchicalFilter()
        self.assertEqual(f.filter_events([]), [])

    def test_drops_short_content(self):
        f = HierarchicalFilter(min_content_length=10)
        events = [
            _ev("ok"),         # 2 chars, dropped
            _ev("this is fine"),  # 12 chars, kept
        ]
        result = f.filter_events(events)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "this is fine")

    def test_drops_pure_observation_without_structured_data(self):
        f = HierarchicalFilter(drop_observation_only=True)
        events = [
            _ev("user typed hello", event_type=EventType.USER_INPUT),
            _ev("ambient noise detected", event_type=EventType.ENV_OBSERVATION, structured=None),
            _ev("temp = 22.5", event_type=EventType.ENV_OBSERVATION, structured={"v": 22.5}),
        ]
        result = f.filter_events(events)
        contents = [e.content for e in result]
        self.assertIn("user typed hello", contents)
        self.assertIn("temp = 22.5", contents)
        self.assertNotIn("ambient noise detected", contents)

    def test_keeps_observation_with_structured_data(self):
        f = HierarchicalFilter(drop_observation_only=True)
        ev_with = _ev("value = 42", event_type=EventType.ENV_OBSERVATION, structured={"v": 42})
        result = f.filter_events([ev_with])
        self.assertEqual(len(result), 1)

    def test_dedup_identical_content(self):
        f = HierarchicalFilter()
        events = [
            _ev("user said hello"),
            _ev("user said hello"),  # dup
            _ev("user said hello"),  # dup
            _ev("user said goodbye"),
        ]
        result = f.filter_events(events)
        contents = [e.content for e in result]
        self.assertEqual(contents, ["user said hello", "user said goodbye"])

    def test_fallback_when_filter_empties_list(self):
        """If every event is dropped, return originals so the LLM still has data."""
        f = HierarchicalFilter(min_content_length=100)  # very strict
        events = [_ev("short"), _ev("also short")]
        result = f.filter_events(events)
        # Fallback: original events returned
        self.assertEqual(len(result), 2)

    def test_preserves_order(self):
        f = HierarchicalFilter()
        events = [_ev(f"event {i}") for i in range(5)]
        result = f.filter_events(events)
        for i, ev in enumerate(result):
            self.assertEqual(ev.content, f"event {i}")


class TestL2KeywordExtraction(unittest.TestCase):
    """extract_keywords returns top-K frequent non-stopword tokens."""

    def test_empty_events(self):
        f = HierarchicalFilter()
        self.assertEqual(f.extract_keywords([]), [])

    def test_simple_frequency(self):
        f = HierarchicalFilter(keyword_top_k=5)
        events = [
            _ev("Alice wants vegetarian food"),
            _ev("Alice likes vegetarian restaurants"),
            _ev("Bob prefers vegetarian food"),
        ]
        kws = f.extract_keywords(events)
        # "vegetarian" appears 3 times -> top
        self.assertEqual(kws[0], "vegetarian")
        # "alice" appears 2 times
        self.assertIn("alice", kws)
        # "food" appears 2 times
        self.assertIn("food", kws)
        # Stop words ("the", "is", etc.) excluded
        for w in kws:
            self.assertNotIn(w, {"a", "the", "is", "and", "or"})

    def test_strips_stop_words(self):
        f = HierarchicalFilter(keyword_top_k=20)
        events = [_ev("the cat and the dog and the bird")]
        kws = f.extract_keywords(events)
        # None of these are stop words
        for w in kws:
            self.assertNotIn(w, {"the", "and", "a", "an"})

    def test_top_k_limit(self):
        f = HierarchicalFilter(keyword_top_k=3)
        events = [_ev("alpha beta gamma delta epsilon zeta")]
        kws = f.extract_keywords(events)
        self.assertEqual(len(kws), 3)

    def test_top_k_zero_returns_empty(self):
        f = HierarchicalFilter(keyword_top_k=0)
        events = [_ev("any content here")]
        self.assertEqual(f.extract_keywords(events), [])

    def test_keyword_hint_format(self):
        f = HierarchicalFilter(keyword_top_k=3)
        events = [_ev("alpha beta gamma")]
        hint = f.keyword_hint(events)
        self.assertTrue(hint.startswith("Key terms: "))
        self.assertIn("alpha", hint)

    def test_keyword_hint_empty_when_no_kws(self):
        f = HierarchicalFilter(keyword_top_k=0)
        events = [_ev("any content")]
        self.assertEqual(f.keyword_hint(events), "")

    def test_case_insensitive(self):
        f = HierarchicalFilter(keyword_top_k=5)
        events = [
            _ev("Vegetarian FOOD"),
            _ev("VEGETarian food"),
        ]
        kws = f.extract_keywords(events)
        # Lowercased and frequency-ranked
        self.assertEqual(kws[0], "vegetarian")
        self.assertIn("food", kws)

    def test_short_tokens_filtered(self):
        f = HierarchicalFilter(keyword_top_k=10)
        events = [_ev("a b c d alpha")]
        kws = f.extract_keywords(events)
        # Only "alpha" should remain (1 char tokens filtered)
        self.assertEqual(kws, ["alpha"])


class TestHierarchicalFilterIntegrationWithLLM(unittest.TestCase):
    """End-to-end: LLMCompressionEngine should pass filtered events to the LLM."""

    def test_llm_receives_filtered_events_and_keyword_hint(self):
        from uams.llm.client import LLMClient
        from uams.pipeline.llm_compression import LLMCompressionEngine

        captured = {}

        class Capture(LLMClient):
            def chat(self, messages, **kwargs):
                captured["messages"] = list(messages)
                return "summary"

        # 5 events: 2 short (dropped), 2 dups (deduped), 1 good
        events = [
            _ev("ok"),  # dropped: too short
            _ev("Alice wants vegetarian food"),
            _ev("Alice wants vegetarian food"),  # dup
            _ev("xx"),  # dropped: too short
            _ev("Bob prefers vegetarian food too"),
        ]

        hf = HierarchicalFilter(min_content_length=5, keyword_top_k=3)
        engine = LLMCompressionEngine(Capture(), hierarchical_filter=hf)
        engine._summarize_batch(events)

        # The user message (second message) should contain only the
        # 2 surviving events AND a "Key terms:" line.
        self.assertEqual(len(captured["messages"]), 2)
        user_msg = captured["messages"][1]["content"]
        self.assertIn("vegetarian", user_msg)
        self.assertIn("Alice", user_msg)
        self.assertIn("Bob", user_msg)
        # No "ok" or "xx" (too-short content)
        self.assertNotIn("ok", user_msg.lower().split("\n")[0])
        # Keyword hint should be present
        self.assertIn("Key terms:", user_msg)

    def test_llm_falls_back_to_original_events_when_filter_empties_list(self):
        from uams.llm.client import LLMClient
        from uams.pipeline.llm_compression import LLMCompressionEngine

        captured = {}

        class Capture(LLMClient):
            def chat(self, messages, **kwargs):
                captured["messages"] = list(messages)
                return "summary"

        # Only short events -> filter empties -> LLM still gets them (fallback)
        events = [_ev("ok"), _ev("xx")]
        hf = HierarchicalFilter(min_content_length=10)
        engine = LLMCompressionEngine(Capture(), hierarchical_filter=hf)
        engine._summarize_batch(events)
        user_msg = captured["messages"][1]["content"]
        # Both original short events are present
        self.assertIn("ok", user_msg)
        self.assertIn("xx", user_msg)


class TestUAMSConfigHierarchical(unittest.TestCase):
    """Config validation for hierarchical compression fields."""

    def test_default_values(self):
        from uams.config import UAMSConfig
        cfg = UAMSConfig()
        self.assertEqual(cfg.hierarchy_min_content_length, 5)
        self.assertTrue(cfg.hierarchy_drop_observation_only)
        self.assertEqual(cfg.hierarchy_keyword_top_k, 10)

    def test_invalid_min_content_length(self):
        from uams.config import UAMSConfig
        with self.assertRaises(ValueError):
            UAMSConfig(hierarchy_min_content_length=0).validate()

    def test_invalid_keyword_top_k(self):
        from uams.config import UAMSConfig
        with self.assertRaises(ValueError):
            UAMSConfig(hierarchy_keyword_top_k=200).validate()

    def test_valid_extremes(self):
        from uams.config import UAMSConfig
        cfg = UAMSConfig(
            hierarchy_min_content_length=1,
            hierarchy_drop_observation_only=False,
            hierarchy_keyword_top_k=0,
        )
        cfg.validate()  # should not raise


if __name__ == "__main__":
    unittest.main()