"""Real-backend integration tests for RedisStore.

Connects to a live Redis server. CI workflow provides one via the
``redis:7-alpine`` service container (see ``.github/workflows/ci.yml``).
Local users can run Redis via ``docker run -d -p 6379:6379 redis:7-alpine``
or set ``UAMS_TEST_REDIS_HOST`` / ``UAMS_TEST_REDIS_PORT`` to an existing
instance. Tests use a unique key prefix per test method so concurrent
runs do not collide.

Run:
    UAMS_TEST_REDIS_HOST=127.0.0.1 \\
    pytest tests/test_redis_store_real.py -v
"""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _redis_available() -> bool:
    try:
        import redis  # noqa: F401
    except ImportError:
        return False
    try:
        import redis
        client = redis.Redis(
            host=os.environ.get("UAMS_TEST_REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("UAMS_TEST_REDIS_PORT", "6379")),
            socket_connect_timeout=3,
        )
        client.ping()
        return True
    except Exception:
        return False


def _redis_args() -> dict:
    return dict(
        host=os.environ.get("UAMS_TEST_REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("UAMS_TEST_REDIS_PORT", "6379")),
        db=0,
    )


def _flush_prefix(prefix: str, redis_client) -> None:
    """Wipe every key under ``prefix*`` so each test starts clean."""
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor=cursor, match=f"{prefix}*", count=200)
        if keys:
            redis_client.delete(*keys)
        if cursor == 0:
            break


def _make_memory(memory_id: str, raw: str = "hello redis", embedding=None,
                mem_type="SEMANTIC", importance: float = 5.0):
    """Build a fully-populated Memory for round-trip tests."""
    from uams.core.models import (
        Memory, MemoryId, TemporalAnchor, MemoryPayload, MemoryMetadata, AgentContext,
    )
    from uams.core.enums import MemoryType, PrivacyLevel
    return Memory(
        id=MemoryId(memory_id),
        anchor=TemporalAnchor(created_at=12345.0, expires_at=None),
        context=AgentContext(
            agent_id="a1", agent_type="t", session_id="s1",
            user_id="u1", team_id="t1", project_id="p1",
        ),
        payload=MemoryPayload(
            raw=raw,
            structured={"source": "test", "tags_match": ["a", "b"]},
            embedding=embedding or [0.1, 0.2, 0.3, 0.4],
        ),
        metadata=MemoryMetadata(
            memory_type=MemoryType[mem_type],
            privacy=PrivacyLevel.PUBLIC,
            importance=importance, confidence=0.95,
            tags={"hello", "test"}, categories={"ci"},
        ),
    )


class TestRedisStoreAvailable(unittest.TestCase):
    """Sanity check: the import path loads and a connection succeeds."""

    def test_redis_library_available(self):
        import redis
        self.assertIsNotNone(redis)


@unittest.skipUnless(_redis_available(), "Redis server not reachable (UAMS_TEST_REDIS_HOST)")
class TestRedisStoreCRUD(unittest.TestCase):
    def setUp(self):
        # Unique key prefix per-test keeps concurrent runs isolated
        prefix = f"uams_test_{uuid.uuid4().hex[:8]}_crud_"
        from uams.storage.redis import RedisStore
        self.prefix = prefix
        self.store = RedisStore(
            **_redis_args(),
            key_prefix=prefix,
            expiry_zset_key=f"{prefix}expiry",
        )
        if not self.store._available:
            self.skipTest("RedisStore._available is False")
        # Pre-test flush in case a previous crashed
        _flush_prefix(prefix, self.store._client)

    def tearDown(self):
        try:
            _flush_prefix(self.prefix, self.store._client)
        finally:
            self.store.close()

    def test_store_and_retrieve_roundtrip(self):
        mem = _make_memory("rt-1", raw="alice vegetarian")
        self.store.store(mem)
        got = self.store.retrieve("rt-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.payload.raw, "alice vegetarian")
        self.assertEqual(got.metadata.importance, 5.0)
        self.assertEqual(got.metadata.tags, {"hello", "test"})
        self.assertEqual(got.payload.structured["source"], "test")
        # embedding roundtrips via pickle — must be a Python list[float]
        self.assertEqual(list(got.payload.embedding), [0.1, 0.2, 0.3, 0.4])

    def test_store_upsert_overrides_existing(self):
        self.store.store(_make_memory("upsert-1", raw="v1"))
        self.store.store(_make_memory("upsert-1", raw="v2", importance=9.0))
        got = self.store.retrieve("upsert-1")
        self.assertEqual(got.payload.raw, "v2")
        self.assertEqual(got.metadata.importance, 9.0)

    def test_retrieve_missing_returns_none(self):
        self.assertIsNone(self.store.retrieve("not-there"))

    def test_delete_existing_returns_true(self):
        self.store.store(_make_memory("del-1"))
        self.assertTrue(self.store.delete("del-1"))
        self.assertIsNone(self.store.retrieve("del-1"))

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.store.delete("not-there"))

    def test_list_all_returns_seeded(self):
        self.store.store(_make_memory("s-1"))
        self.store.store(_make_memory("s-2"))
        self.store.store(_make_memory("s-3"))
        results = self.store.list_all(limit=10)
        ids = {str(m.id) for m in results}
        self.assertSetEqual(ids, {"s-1", "s-2", "s-3"})


@unittest.skipUnless(_redis_available(), "Redis server not reachable")
class TestRedisStoreSearch(unittest.TestCase):

    def setUp(self):
        prefix = f"uams_test_{uuid.uuid4().hex[:8]}_search_"
        from uams.storage.redis import RedisStore
        self.prefix = prefix
        self.store = RedisStore(
            **_redis_args(),
            key_prefix=prefix,
            expiry_zset_key=f"{prefix}expiry",
        )
        if not self.store._available:
            self.skipTest("RedisStore._available is False")
        _flush_prefix(prefix, self.store._client)
        for label, raw in (("alice", "alice loves vegetarian food"),
                          ("bob", "bob prefers meat recipes"),
                          ("carol", "carol enjoys gardening on weekends")):
            self.store.store(_make_memory(label, raw=raw))

    def tearDown(self):
        try:
            _flush_prefix(self.prefix, self.store._client)
        finally:
            self.store.close()

    def test_search_keywords_finds_vegetarian(self):
        results = self.store.search_keywords("vegetarian", k=5)
        ids = [str(m.id) for m in results]
        self.assertIn("alice", ids)
        self.assertNotIn("bob", ids)

    def test_search_vector_falls_back_to_recent(self):
        results = self.store.search_vector([0.1, 0.2, 0.3], k=2)
        # Redis has no native vector index; expected behavior is recency fallback
        self.assertGreater(len(results), 0)
        self.assertLessEqual(len(results), 2)


@unittest.skipUnless(_redis_available(), "Redis server not reachable")
class TestRedisStoreClose(unittest.TestCase):
    def test_close_is_idempotent(self):
        prefix = f"uams_test_{uuid.uuid4().hex[:8]}_close_"
        from uams.storage.redis import RedisStore
        store = RedisStore(
            **_redis_args(),
            key_prefix=prefix,
            expiry_zset_key=f"{prefix}expiry",
        )
        if not store._available:
            self.skipTest("RedisStore._available is False")
        store.close()
        # Calling close again should not raise
        store.close()
        _flush_prefix(prefix, store._client)


if __name__ == "__main__":
    unittest.main()
