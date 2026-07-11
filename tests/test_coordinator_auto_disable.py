"""Tests for multi_agent.coordinator — Redis-failure auto-disable.

When the Redis distributed lock raises, the coordinator must not
silently degrade to in-memory locking (that would mislead the caller
in a multi-process deployment). Instead, the coordinator marks itself
disabled and future acquire_lease / release_lease calls short-circuit
to None / False. Other workers are unaffected because each has its
own MultiAgentCoordinator instance.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.multi_agent.coordinator import MultiAgentCoordinator
from uams.storage.memory import InMemoryStore


def _make_redis_failing_client():
    """A redis_client mock that mimics the shape MultiAgentCoordinator
    checks for, but raises ConnectionError on every lock call."""
    client = MagicMock()
    client._available = True
    client._client = MagicMock()
    client._client.set.side_effect = ConnectionError("simulated redis down")
    client._client.get.side_effect = ConnectionError("simulated redis down")
    client._client.delete.side_effect = ConnectionError("simulated redis down")
    return client


class TestCoordinatorAutoDisable(unittest.TestCase):

    def test_initially_not_disabled(self):
        store = InMemoryStore(max_capacity=10)
        coord = MultiAgentCoordinator(store)
        self.assertFalse(coord.is_disabled)

    def test_redis_acquire_failure_disables_coordinator(self):
        """If acquire_lease hits a Redis error, coordinator is disabled
        and the call returns None (not a fake in-memory lease)."""
        store = InMemoryStore(max_capacity=10)
        coord = MultiAgentCoordinator(
            store, redis_client=_make_redis_failing_client()
        )
        result = coord.acquire_lease("agent-A", "resource-1")
        self.assertIsNone(result)
        self.assertTrue(coord.is_disabled)

    def test_redis_release_failure_disables_coordinator(self):
        """If release_lease hits a Redis error, coordinator is disabled."""
        store = InMemoryStore(max_capacity=10)
        coord = MultiAgentCoordinator(
            store, redis_client=_make_redis_failing_client()
        )
        result = coord.release_lease("agent-A", "resource-1")
        self.assertFalse(result)
        self.assertTrue(coord.is_disabled)

    def test_disabled_acquire_short_circuits(self):
        """After disable, future acquire_lease does not call Redis at all."""
        store = InMemoryStore(max_capacity=10)
        redis = _make_redis_failing_client()
        coord = MultiAgentCoordinator(store, redis_client=redis)

        # First call: triggers disable
        coord.acquire_lease("agent-A", "resource-1")
        self.assertTrue(coord.is_disabled)
        # Reset the mock so we can assert that no further calls happen
        redis._client.set.reset_mock()
        redis._client.get.reset_mock()

        # Subsequent calls must short-circuit and not touch Redis
        result = coord.acquire_lease("agent-B", "resource-2")
        self.assertIsNone(result)
        redis._client.set.assert_not_called()

    def test_disabled_release_short_circuits(self):
        """After disable, future release_lease does not call Redis."""
        store = InMemoryStore(max_capacity=10)
        redis = _make_redis_failing_client()
        coord = MultiAgentCoordinator(store, redis_client=redis)
        coord.acquire_lease("agent-A", "resource-1")  # triggers disable
        self.assertTrue(coord.is_disabled)
        redis._client.set.reset_mock()
        redis._client.get.reset_mock()

        result = coord.release_lease("agent-A", "resource-1")
        self.assertFalse(result)
        redis._client.get.assert_not_called()
        redis._client.delete.assert_not_called()

    def test_in_memory_path_does_not_disable(self):
        """When no Redis is provided, a Redis-style failure cannot happen.
        But also: in-memory mode must not be flipped off by phantom errors.
        A lease must still be acquired via the in-memory fallback."""
        store = InMemoryStore(max_capacity=10)
        coord = MultiAgentCoordinator(store)  # no redis_client
        result = coord.acquire_lease("agent-A", "resource-1")
        self.assertIsNotNone(result)
        self.assertFalse(coord.is_disabled)

    def test_disable_is_idempotent(self):
        """Calling _disable twice is harmless (no duplicate log spam)."""
        store = InMemoryStore(max_capacity=10)
        coord = MultiAgentCoordinator(
            store, redis_client=_make_redis_failing_client()
        )
        # Trigger disable via acquire
        coord.acquire_lease("agent-A", "resource-1")
        # Second disable attempt: no exception, _disabled already True
        coord._disable(RuntimeError("second"))
        self.assertTrue(coord.is_disabled)


if __name__ == "__main__":
    unittest.main()
