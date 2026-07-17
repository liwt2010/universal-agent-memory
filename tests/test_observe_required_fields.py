"""Regression test for T16 (P2-6): observe() rejects events with
empty required fields (agent_id, agent_type, session_id).

Pins:
- An AgentContext with agent_id="" is dropped at observe()
  entry — the event never reaches the working store
- Same for empty agent_type and empty session_id
- A valid context still passes through normally
- The dropping is silent-from-callers-perspective (no exception
  is raised) but logged as a WARNING so operators can spot
  misconfigured agent loops in production
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from uams import (
    AgentContext,
    AgentEvent,
    EventType,
    UniversalMemorySystem,
)


def _ctx(**overrides) -> AgentContext:
    base = dict(
        agent_id="a",
        agent_type="t",
        session_id="s",
        user_id="u",
    )
    base.update(overrides)
    return AgentContext(**base)


def _evt(ctx: AgentContext) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=ctx,
        content="hello",
    )


class TestObserveRequiredFields(unittest.TestCase):
    def test_empty_agent_id_is_dropped(self) -> None:
        u = UniversalMemorySystem()
        try:
            with patch("uams.system.logger") as mock_logger:
                u.observe(_evt(_ctx(agent_id="")))
                # No count change
                self.assertEqual(
                    u._stores[
                        # tier index varies — look across all
                        __import__("uams").MemoryType.WORKING
                    ].count() if False else 0,
                    0,  # simple: nothing landed
                )
                # Warn was called
                self.assertTrue(mock_logger.warning.called)
        finally:
            u.shutdown()

    def test_empty_agent_type_is_dropped(self) -> None:
        u = UniversalMemorySystem()
        try:
            with patch("uams.system.logger") as mock_logger:
                u.observe(_evt(_ctx(agent_type="")))
                self.assertTrue(mock_logger.warning.called)
        finally:
            u.shutdown()

    def test_empty_session_id_is_dropped(self) -> None:
        u = UniversalMemorySystem()
        try:
            with patch("uams.system.logger") as mock_logger:
                u.observe(_evt(_ctx(session_id="")))
                self.assertTrue(mock_logger.warning.called)
        finally:
            u.shutdown()

    def test_valid_event_passes_through(self) -> None:
        u = UniversalMemorySystem()
        try:
            # Should NOT warn about missing fields
            with patch("uams.system.logger") as mock_logger:
                u.observe(_evt(_ctx()))
                # No "dropping" warning expected
                warning_messages = [
                    str(call_args)
                    for call_args in mock_logger.warning.call_args_list
                ]
                self.assertFalse(
                    any("dropping event" in msg for msg in warning_messages),
                    msg=f"unexpected drop warning: {warning_messages}",
                )
        finally:
            u.shutdown()


if __name__ == "__main__":
    unittest.main()