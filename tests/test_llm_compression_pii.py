"""Regression test for T11 (P1-7): LLMCompressionEngine routes
the assembled narrative through PrivacyFilter before it lands in
the episodic store.

Pins:
- An LLM output that contains an OpenAI key is redacted before
  being stored in Memory.payload.raw
- An LLM output that contains an email is redacted when ANY
  source event was PRIVATE
- The compressed memory's privacy level is the MAX across source
  events (not just the first event's privacy)
- The structured payload field is unchanged
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from uams.core.enums import EventType, MemoryType, PrivacyLevel
from uams.core.models import (
    AgentContext,
    AgentEvent,
    MemoryId,
    TemporalAnchor,
)
from uams.llm.client import LLMClient
from uams.pipeline.llm_compression import LLMCompressionEngine


def _ctx() -> AgentContext:
    return AgentContext(
        agent_id="a", agent_type="t", session_id="s",
        user_id="u",
    )


def _evt(content: str, privacy: PrivacyLevel = PrivacyLevel.PUBLIC) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=_ctx(),
        content=content,
        privacy=privacy,
    )


class TestLLMCompressionPrivacy(unittest.TestCase):
    def _build_engine_with_mock_llm(self, llm_response: str) -> LLMCompressionEngine:
        mock_llm = MagicMock(spec=LLMClient)
        mock_llm.chat.return_value = llm_response
        return LLMCompressionEngine(mock_llm, max_events_per_call=10)

    def test_openai_key_in_llm_output_is_redacted(self) -> None:
        llm = (
            "Session summary: alice used key sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL "
            "to bootstrap a project."
        )
        eng = self._build_engine_with_mock_llm(llm)
        events = [_evt("hello")]
        mem = eng.compress_working_to_episodic(events)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", mem.payload.raw)
        self.assertIn("<OPENAI_API_KEY>", mem.payload.raw)

    def test_email_in_llm_output_redacted_when_any_event_private(self) -> None:
        llm = "Alice reached out to alice@example.com for follow-up."
        eng = self._build_engine_with_mock_llm(llm)
        # First event PUBLIC, second event PRIVATE — max should be PRIVATE
        events = [_evt("hi", PrivacyLevel.PUBLIC), _evt("secret", PrivacyLevel.PRIVATE)]
        mem = eng.compress_working_to_episodic(events)
        self.assertNotIn("alice@example.com", mem.payload.raw)
        self.assertIn("<EMAIL>", mem.payload.raw)
        # The compressed memory inherits the max privacy level
        self.assertEqual(mem.metadata.privacy, PrivacyLevel.PRIVATE)

    def test_public_only_events_preserve_email_in_output(self) -> None:
        llm = "Summary: ping alice@example.com about status."
        eng = self._build_engine_with_mock_llm(llm)
        events = [_evt("hi", PrivacyLevel.PUBLIC)]
        mem = eng.compress_working_to_episodic(events)
        # PUBLIC-only source → PUBLIC output → email NOT scrubbed
        # (only secrets are scrubbed at PUBLIC, see PrivacyFilter)
        self.assertIn("alice@example.com", mem.payload.raw)

    def test_structured_payload_preserved(self) -> None:
        eng = self._build_engine_with_mock_llm("clean summary")
        events = [_evt("hi")]
        mem = eng.compress_working_to_episodic(events)
        self.assertEqual(mem.payload.structured["compression_engine"], "llm")
        self.assertEqual(mem.payload.structured["event_count"], 1)

    def test_compressed_memory_has_episodic_type(self) -> None:
        eng = self._build_engine_with_mock_llm("summary")
        events = [_evt("hi")]
        mem = eng.compress_working_to_episodic(events)
        self.assertEqual(mem.metadata.memory_type, MemoryType.EPISODIC)


if __name__ == "__main__":
    unittest.main()