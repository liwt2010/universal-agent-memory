"""Tests for MultiAgentCoordinator signal queue bound.

P1-CON-1 regression: read_signals() only marks signals as read; it
never removed them from the queue. Long-running agents that emit
broadcast signals faster than they are consumed would see unbounded
memory growth. The fix caps the queue at MAX_SIGNALS and drops the
oldest entries on append.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.multi_agent.coordinator import MultiAgentCoordinator, Signal
from uams.storage.memory import InMemoryStore


class TestSignalQueueBound(unittest.TestCase):
    def _coordinator(self):
        return MultiAgentCoordinator(shared_store=InMemoryStore())

    def test_below_cap_no_drops(self):
        c = self._coordinator()
        for i in range(10):
            c.send_signal(Signal(sender="a", recipient="b", signal_type="t"))
        self.assertEqual(len(c._signals), 10)

    def test_at_cap_no_drops(self):
        """Sending exactly MAX_SIGNALS must keep all of them."""
        c = self._coordinator()
        for i in range(c.MAX_SIGNALS):
            c.send_signal(Signal(sender="a", recipient="b", signal_type="t"))
        self.assertEqual(len(c._signals), c.MAX_SIGNALS)

    def test_above_cap_drops_oldest(self):
        """Sending MAX_SIGNALS + 100 must keep only the newest MAX_SIGNALS."""
        c = self._coordinator()
        # Tag each signal so we can verify which survived.
        for i in range(c.MAX_SIGNALS + 100):
            c.send_signal(Signal(sender=f"a-{i}", recipient="b", signal_type="t"))
        self.assertEqual(len(c._signals), c.MAX_SIGNALS)
        # The oldest 100 (a-0..a-99) should be gone; the newest
        # (a-100..a-MAX_SIGNALS+99) should remain.
        first_sender = c._signals[0].sender
        last_sender = c._signals[-1].sender
        self.assertEqual(first_sender, f"a-100")
        self.assertEqual(last_sender, f"a-{c.MAX_SIGNALS + 99}")

    def test_unread_signals_after_overflow(self):
        """After overflow, a fresh consumer can still read the latest signals."""
        c = self._coordinator()
        for i in range(c.MAX_SIGNALS + 5):
            c.send_signal(Signal(
                sender=f"a-{i}", recipient="reader", signal_type="ping",
                payload={"i": i},
            ))
        received = c.read_signals("reader")
        # The reader sees only what's currently in the queue (none of
        # the dropped 5 oldest), and gets exactly the broadcast count.
        self.assertEqual(len(received), c.MAX_SIGNALS)
        # The first one read should be a-5 (the oldest surviving).
        self.assertEqual(received[0].payload["i"], 5)


if __name__ == "__main__":
    unittest.main()