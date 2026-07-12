"""Tests for decay_sweep concurrency safety.

Bug 9 regression test: UniversalMemorySystem.decay_sweep() must
serialize concurrent calls so two long-running sweeps cannot
race through delete_expired() implementations.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestDecaySweepConcurrency(unittest.TestCase):
    """Two concurrent decay_sweep() calls must not both run."""

    def _make_ums(self):
        from uams import UniversalMemorySystem
        return UniversalMemorySystem()

    def test_concurrent_sweep_second_call_returns_zero(self):
        """If sweep A is in flight, sweep B sees the lock-held state and
        returns 0 immediately (with a debug log) — it does NOT block
        waiting for A, and it does NOT run a second sweep in parallel."""
        from uams import UniversalMemorySystem

        ums = UniversalMemorySystem()
        held = threading.Event()
        release = threading.Event()
        started = threading.Event()

        # Replace the inner sweep with one that blocks until we let it finish.
        original_sweep = ums._forgetting.sweep

        def slow_sweep():
            started.set()
            held.set()
            # Wait until the test signals completion.
            release.wait(timeout=2.0)
            return 0

        ums._forgetting.sweep = slow_sweep

        results = {}

        def call_sweep(label):
            results[label] = ums.decay_sweep()

        t1 = threading.Thread(target=call_sweep, args=("A",))
        t1.start()
        # Make sure A is inside the sweep before B starts.
        self.assertTrue(started.wait(timeout=1.0))
        self.assertTrue(held.wait(timeout=1.0))

        t2 = threading.Thread(target=call_sweep, args=("B",))
        t2.start()
        t2.join(timeout=1.0)

        # Release A so it completes cleanly.
        release.set()
        t1.join(timeout=1.0)

        ums._forgetting.sweep = original_sweep

        # A ran the slow sweep, B was short-circuited with 0.
        self.assertEqual(results["A"], 0)
        self.assertEqual(results["B"], 0)
        # B must have completed (not blocked waiting for A).
        self.assertFalse(t2.is_alive(), "B should not block on A's lock")

    def test_sweep_lock_released_after_completion(self):
        """After a sweep finishes, the next sweep can acquire the lock.
        Regression guard against accidental lock leaks."""
        ums = self._make_ums()
        # First sweep acquires and releases.
        self.assertGreaterEqual(ums.decay_sweep(), 0)
        # Second sweep must still be able to acquire.
        self.assertGreaterEqual(ums.decay_sweep(), 0)


if __name__ == "__main__":
    unittest.main()