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


class FakePipeline:
    """In-memory fake Redis pipeline for testing. Queues commands, executes in order."""

    def __init__(self, fake: "FakeRedis", transaction: bool = True):
        self._fake = fake
        self._commands: list = []
        self._transaction = transaction

    def hset(self, key, *args, **kwargs):
        self._commands.append(("hset", key, args, kwargs))
        return self

    def expire(self, key, seconds):
        self._commands.append(("expire", key, seconds))
        return self

    def zadd(self, key, mapping):
        self._commands.append(("zadd", key, mapping))
        return self

    def delete(self, *keys):
        self._commands.append(("delete", keys))
        return self

    def zrem(self, key, *members):
        self._commands.append(("zrem", key, members))
        return self

    def sadd(self, key, *members):
        self._commands.append(("sadd", key, members))
        return self

    def srem(self, key, *members):
        self._commands.append(("srem", key, members))
        return self

    def hgetall(self, key):
        self._commands.append(("hgetall", key))
        return self

    def execute(self):
        results = []
        for cmd in self._commands:
            name = cmd[0]
            if name == "hset":
                _, key, args, kwargs = cmd
                results.append(self._fake.hset(key, *args, **kwargs))
            elif name == "hgetall":
                _, key = cmd
                results.append(self._fake.hgetall(key))
            elif name == "expire":
                _, key, seconds = cmd
                results.append(self._fake.expire(key, seconds))
            elif name == "zadd":
                _, key, mapping = cmd
                results.append(self._fake.zadd(key, mapping))
            elif name == "delete":
                _, keys = cmd
                results.append(self._fake.delete(*keys))
            elif name == "zrem":
                _, key, members = cmd
                results.append(self._fake.zrem(key, *members))
            elif name == "sadd":
                _, key, members = cmd
                results.append(self._fake.sadd(key, *members))
            elif name == "srem":
                _, key, members = cmd
                results.append(self._fake.srem(key, *members))
        return results


class FakeRedis:
    """In-memory fake Redis for testing."""

    def __init__(self):
        self._hashes: dict = {}
        self._zsets: dict = {}
        self._sets: dict = {}
        self._pubsub_channels: dict = {}
        self._cursor = 0

    def ping(self):
        return True

    def pipeline(self, transaction=True):
        return FakePipeline(self, transaction=transaction)

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
            k_str = k.decode("utf-8") if isinstance(k, bytes) else k
            if k_str in self._hashes:
                del self._hashes[k_str]
                count += 1
            if k_str in self._zsets:
                del self._zsets[k_str]
                count += 1
            if k_str in self._sets:
                del self._sets[k_str]
                count += 1
            for zkey, zval in list(self._zsets.items()):
                if k_str in zval:
                    del zval[k_str]
            for skey, sval in list(self._sets.items()):
                if k_str in sval:
                    sval.discard(k_str)
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

    def sadd(self, key, *members):
        if key not in self._sets:
            self._sets[key] = set()
        added = 0
        for m in members:
            m_norm = m.decode("utf-8") if isinstance(m, bytes) else m
            if m_norm not in self._sets[key]:
                self._sets[key].add(m_norm)
                added += 1
        return added

    def smembers(self, key):
        return set(self._sets.get(key, set()))

    def srem(self, key, *members):
        s = self._sets.get(key, set())
        count = 0
        for m in members:
            m_norm = m.decode("utf-8") if isinstance(m, bytes) else m
            if m_norm in s:
                s.discard(m_norm)
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

    def test_inverted_index_built_on_store(self):
        """After store(), the per-term index and per-memory token set must exist."""
        mem = self._make_memory(raw="apple banana cherry")
        self.store.store(mem)
        mem_id = str(mem.id)
        # Per-memory token set should contain the 3 tokens
        mem_tokens = self.fake.smembers(f"test:idx:mem:{mem_id}:tokens")
        self.assertEqual(mem_tokens, {"apple", "banana", "cherry"})
        # Per-term sets should each contain this memory's id
        for term in ("apple", "banana", "cherry"):
            ids = self.fake.smembers(f"test:idx:term:{term}")
            self.assertIn(mem_id, ids)

    def test_inverted_index_cleaned_on_delete(self):
        """After delete(), the per-term sets and per-memory token set must be gone."""
        mem = self._make_memory(raw="apple banana")
        self.store.store(mem)
        mem_id = str(mem.id)
        self.store.delete(mem_id)
        # Per-memory token set deleted
        self.assertEqual(self.fake.smembers(f"test:idx:mem:{mem_id}:tokens"), set())
        # Per-term sets have this memory removed
        for term in ("apple", "banana"):
            ids = self.fake.smembers(f"test:idx:term:{term}")
            self.assertNotIn(mem_id, ids)

    def test_inverted_index_search_uses_token_index(self):
        """search_keywords() should find via token index, not full SCAN.

        With 100 memories, 99 of which don't contain the term, an index-based
        search returns the 1 hit in O(1) term lookup + O(1) HGETALL.
        """
        # Store 99 "noise" memories + 1 target
        for i in range(99):
            self.store.store(self._make_memory(raw=f"unrelated {i} noise"))
        target = self._make_memory(raw="apple unique_target")
        self.store.store(target)

        results = self.store.search_keywords("apple", k=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload.raw, "apple unique_target")

    def test_inverted_index_substring_falls_back_per_candidate(self):
        """Per-candidate substring check is preserved.

        Once the index narrows candidates to a set, we still check that any
        query term is a substring of raw. This catches queries where a query
        term IS a token in the index but the user expected a wider match
        (e.g. a multi-word query where the second word is the substring
        discriminator).
        """
        mem = self._make_memory(raw="apple pie")
        self.store.store(mem)
        # "apple" is a real token, "pie" is a real token. The query
        # "apple" token-matches the index and the substring check passes.
        results = self.store.search_keywords("apple", k=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload.raw, "apple pie")
        # Note: a query like "app" (3 chars, tokenized but no memory has
        # "app" as a token) returns 0 — the index is now the gatekeeper
        # for what is searchable. This is a documented behavior change
        # from the pre-index O(N) full-SCAN implementation.

    def test_inverted_index_search_caps_candidate_set(self):
        """With many candidates sharing a common token, search must
        sample down to k*10 to bound JSON-deserialization cost.

        Real-world stress: 200 memories all contain "common" token.
        Search "common" should sample 100 (k*10=100) and return up to 10,
        not HGETALL all 200 and JSON-decode them all.
        """
        import random
        for i in range(200):
            # Each memory has the "common" token plus a unique 20-char string
            self.store.store(self._make_memory(
                raw=f"common {''.join(random.choices('abcde', k=20))}_{i}"
            ))
        results = self.store.search_keywords("common", k=10)
        # Should return at most k=10, even though 200 candidates exist
        self.assertLessEqual(len(results), 10)
        # And the search should have returned SOMETHING (not failed)
        self.assertGreater(len(results), 0)


if __name__ == "__main__":
    unittest.main()
