"""Mock-based unit tests for RedisStore.

Tests RedisStore logic without requiring a real Redis server.
Uses unittest.mock to simulate redis.Redis client behavior.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Inject mock redis module before importing RedisStore
_mock_redis = MagicMock()
_mock_redis.Redis = MagicMock
sys.modules["redis"] = _mock_redis

from uams.storage.redis import RedisStore
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata, Relation,
)
from uams.core.enums import MemoryType, PrivacyLevel


class FakeRedis:
    """In-memory fake Redis for testing."""

    def __init__(self):
        self._hashes: dict = {}
        self._zsets: dict = {}
        self._pubsub_channels: dict = {}
        self._cursor = 0

    def ping(self):
        return True

    def hset(self, key, *args, **kwargs):
        if key not in self._hashes:
            self._hashes[key] = {}
        if len(args) == 1 and isinstance(args[0], dict):
            mapping = args[0]
            self._hashes[key].update(mapping)
            return len(mapping)
        elif len(args) == 2:
            field, value = args
            self._hashes[key][field] = value
            return 1
        elif kwargs.get("mapping"):
            mapping = kwargs["mapping"]
            self._hashes[key].update(mapping)
            return len(mapping)
        return 0

    def hgetall(self, key):
        return self._hashes.get(key, {})

    def hset_single(self, key, field, value):
        if key not in self._hashes:
            self._hashes[key] = {}
        self._hashes[key][field] = value

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self._hashes:
                del self._hashes[k]
                count += 1
            for zkey, zval in list(self._zsets.items()):
                if k.decode() if isinstance(k, bytes) else k in zval:
                    del zval[k.decode() if isinstance(k, bytes) else k]
        return count

    def zadd(self, key, mapping):
        if key not in self._zsets:
            self._zsets[key] = {}
        for member, score in mapping.items():
            self._zsets[key][member] = score
        return len(mapping)

    def zrangebyscore(self, key, min_score, max_score):
        z = self._zsets.get(key, {})
        result = []
        for member, score in z.items():
            if min_score <= score <= max_score:
                result.append(member)
        return result

    def zrem(self, key, *members):
        z = self._zsets.get(key, {})
        count = 0
        for m in members:
            m_str = m.decode() if isinstance(m, bytes) else m
            if m_str in z:
                del z[m_str]
                count += 1
        return count

    def expire(self, key, seconds):
        return True

    def scan(self, cursor, match=None, count=None):
        keys = list(self._hashes.keys())
        return 0, keys

    def publish(self, channel, message):
        if channel not in self._pubsub_channels:
            self._pubsub_channels[channel] = []
        self._pubsub_channels[channel].append(message)
        return 1

    def pubsub(self):
        mock = MagicMock()
        mock.subscribe = MagicMock(return_value=None)
        return mock


class TestRedisStoreMock(unittest.TestCase):

    def setUp(self):
        self.fake = FakeRedis()
        # Ensure redis mock module is available for RedisStore.__init__
        if "redis" not in sys.modules:
            _mock_redis = MagicMock()
        else:
            _mock_redis = sys.modules["redis"]
        _mock_redis.Redis = lambda **kwargs: self.fake
        _mock_redis.ConnectionPool = lambda **kwargs: None
        sys.modules["redis"] = _mock_redis
        self.store = RedisStore(host="localhost", port=6379, db=0, key_prefix="test:")
        self.assertTrue(self.store._available)

    def tearDown(self):
        pass

    def _make_memory(self, raw="hello", mem_type=MemoryType.WORKING, tags=None, relations=None, expires_at=None):
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(created_at=1000.0, expires_at=expires_at),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw=raw),
            metadata=MemoryMetadata(
                memory_type=mem_type,
                privacy=PrivacyLevel.PUBLIC,
                tags=set(tags or []),
                relations=relations or [],
            ),
        )

    def test_store_and_retrieve(self):
        mem = self._make_memory(raw="store me")
        self.store.store(mem)
        retrieved = self.store.retrieve(str(mem.id))
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.payload.raw, "store me")
        self.assertEqual(retrieved.metadata.memory_type, MemoryType.WORKING)

    def test_delete(self):
        mem = self._make_memory(raw="delete me")
        self.store.store(mem)
        self.assertTrue(self.store.delete(str(mem.id)))
        self.assertIsNone(self.store.retrieve(str(mem.id)))
        self.assertFalse(self.store.delete(str(mem.id)))

    def test_search_keywords(self):
        self.store.store(self._make_memory(raw="apple banana"))
        self.store.store(self._make_memory(raw="cherry date"))
        self.store.store(self._make_memory(raw="apple pie"))

        results = self.store.search_keywords("apple", k=10)
        self.assertEqual(len(results), 2)
        raws = {m.payload.raw for m in results}
        self.assertIn("apple banana", raws)
        self.assertIn("apple pie", raws)

    def test_search_vector_fallback(self):
        """Vector search should fall back to recent memories."""
        self.store.store(self._make_memory(raw="vector test"))
        results = self.store.search_vector([0.1, 0.2], k=10)
        self.assertTrue(len(results) >= 1)

    def test_search_graph(self):
        mem_a = self._make_memory(raw="node A", relations=[Relation("links", "target_b", 1.0)])
        mem_b = self._make_memory(raw="node B")
        self.store.store(mem_a)
        self.store.store(mem_b)

        results = self.store.search_graph(str(mem_a.id), depth=2)
        self.assertTrue(len(results) >= 1)

    def test_delete_expired(self):
        import time
        expired = self._make_memory(raw="expired", expires_at=time.time() - 100)
        fresh = self._make_memory(raw="fresh", expires_at=time.time() + 10000)
        self.store.store(expired)
        self.store.store(fresh)
        count = self.store.delete_expired()
        self.assertGreaterEqual(count, 1)
        self.assertIsNone(self.store.retrieve(str(expired.id)))
        self.assertIsNotNone(self.store.retrieve(str(fresh.id)))

    def test_publish_and_subscribe(self):
        self.store._enable_pubsub = True
        self.assertTrue(self.store.publish_signal("agent_ch", {"type": "alert"}))
        pubsub = self.store.subscribe_signals("agent_ch")
        self.assertIsNotNone(pubsub)

    def test_list_all(self):
        self.store.store(self._make_memory(raw="one"))
        self.store.store(self._make_memory(raw="two"))
        results = self.store.list_all(limit=10)
        self.assertTrue(len(results) >= 2)


if __name__ == "__main__":
    unittest.main()
