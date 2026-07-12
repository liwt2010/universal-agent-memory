"""Tests for ConsolidateResult and the new consolidate() return type.

Bug 2 regression: previously consolidate() returned None and produced no
telemetry. Vault and other callers had to peek at private state
(system._session_events) to derive source_event_count, which is
thread-unsafe. The fix returns a ConsolidateResult with structured
fields.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestConsolidateResult(unittest.TestCase):
    def _make(self):
        from uams import UniversalMemorySystem
        from uams.core.enums import EventType
        from uams.core.models import AgentContext, AgentEvent
        ums = UniversalMemorySystem()
        ctx = AgentContext(
            agent_id="a", agent_type="t", session_id="s1", user_id="u",
        )
        for i in range(3):
            ums.observe(AgentEvent(
                event_type=EventType.USER_INPUT,
                agent_context=ctx,
                content=f"event {i}",
            ))
        return ums, ctx

    def test_returns_consolidate_result_instance(self):
        from uams.system import ConsolidateResult
        ums, _ = self._make()
        result = ums.consolidate(session_id="s1")
        self.assertIsInstance(result, ConsolidateResult)
        self.assertEqual(result.session_id, "s1")

    def test_reports_source_event_count(self):
        ums, _ = self._make()
        result = ums.consolidate(session_id="s1")
        self.assertEqual(result.source_event_count, 3)

    def test_reports_episodic_memory_id(self):
        ums, _ = self._make()
        result = ums.consolidate(session_id="s1")
        # Episodic is produced by working->episodic step.
        self.assertIsNotNone(result.episodic_memory_id)

    def test_empty_session_returns_zero_count_not_error(self):
        """No events to consolidate is a successful no-op, not an error."""
        ums = type(self)._make_ums_empty() if hasattr(type(self), "_make_ums_empty") else None
        # Build a fresh one.
        from uams import UniversalMemorySystem
        ums = UniversalMemorySystem()
        result = ums.consolidate(session_id="never-existed")
        self.assertEqual(result.source_event_count, 0)
        self.assertIsNone(result.error)
        self.assertIsNone(result.episodic_memory_id)

    def test_consolidate_all_aggregates(self):
        """consolidate() with no session_id iterates and sums durations."""
        from uams import UniversalMemorySystem
        from uams.core.enums import EventType
        from uams.core.models import AgentContext, AgentEvent
        ums = UniversalMemorySystem()
        for sid in ("s1", "s2"):
            ctx = AgentContext(agent_id="a", agent_type="t", session_id=sid)
            for i in range(2):
                ums.observe(AgentEvent(
                    event_type=EventType.USER_INPUT,
                    agent_context=ctx,
                    content=f"event {i}",
                ))
        result = ums.consolidate()
        # Two sessions × 2 events = 4 total source events aggregated.
        self.assertEqual(result.source_event_count, 4)

    def test_consolidate_does_not_raise_on_partial_failure(self):
        """Even if a tier fails, consolidate() returns a result with error populated."""
        from uams import UniversalMemorySystem
        from uams.core.enums import EventType
        from uams.core.models import AgentContext, AgentEvent

        ums = UniversalMemorySystem()
        ctx = AgentContext(agent_id="a", agent_type="t", session_id="s-err")
        ums.observe(AgentEvent(
            event_type=EventType.USER_INPUT,
            agent_context=ctx,
            content="normal event",
        ))
        # Sabotage one of the stores so episodic store fails.
        ums._stores[__import__("uams").core.enums.MemoryType.EPISODIC].store = lambda m: (_ for _ in ()).throw(
            RuntimeError("simulated store failure")
        )
        result = ums.consolidate(session_id="s-err")
        # consolidate() did not raise — it returned a populated result.
        self.assertIsNotNone(result)
        self.assertEqual(result.source_event_count, 1)
        # Episodic step failed → episodic_memory_id is None and error is set.
        self.assertIsNone(result.episodic_memory_id)
        self.assertIn("working_to_episodic", (result.error or ""))


if __name__ == "__main__":
    unittest.main()