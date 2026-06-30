"""Comprehensive tests for UAMS production-grade components."""

import sys
import os
import unittest
import threading
import time
import tempfile
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import (
    MemoryType,
    EventType,
    PrivacyLevel,
    MemoryId,
    TemporalAnchor,
    AgentContext,
    MemoryPayload,
    MemoryMetadata,
    Memory,
    AgentEvent,
    InMemoryStore,
    HeuristicCompressionEngine,
    PrivacyFilter,
    DeduplicationWindow,
    ForgettingEngine,
    UniversalMemorySystem,
)
from uams.config import UAMSConfig
from uams.storage.sqlite import SQLiteStore
from uams.health import MetricsCollector, HealthServer
from uams.utils.tokens import TokenEstimator, estimate_tokens


class TestMemoryId(unittest.TestCase):
    def test_unique(self):
        a = MemoryId()
        b = MemoryId()
        self.assertNotEqual(a.id, b.id)

    def test_from_string(self):
        a = MemoryId("custom-123")
        self.assertEqual(a.id, "custom-123")


class TestTemporalAnchor(unittest.TestCase):
    def test_not_expired(self):
        anchor = TemporalAnchor(expires_at=time.time() + 1000)
        self.assertFalse(anchor.is_expired())

    def test_expired(self):
        anchor = TemporalAnchor(expires_at=time.time() - 1)
        self.assertTrue(anchor.is_expired())


class TestInMemoryStore(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()

    def test_store_and_retrieve(self):
        mem = self._make_memory("hello world")
        self.store.store(mem)
        fetched = self.store.retrieve(str(mem.id))
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.payload.raw, "hello world")

    def test_search_keywords(self):
        self.store.store(self._make_memory("Alice likes tea"))
        self.store.store(self._make_memory("Bob likes coffee"))
        results = self.store.search_keywords("Alice tea", k=5)
        self.assertEqual(len(results), 1)
        self.assertIn("Alice", results[0].payload.raw)

    def test_delete_expired(self):
        mem = self._make_memory("temp")
        mem.anchor.expires_at = time.time() - 1
        self.store.store(mem)
        self.assertEqual(self.store.delete_expired(), 1)
        self.assertIsNone(self.store.retrieve(str(mem.id)))

    def test_thread_safety(self):
        """Verify InMemoryStore is thread-safe under concurrent writes."""
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    mem = self._make_memory(f"worker_{n}_item_{i}")
                    self.store.store(mem)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Thread safety errors: {errors}")
        # Verify at least some data was stored
        all_mems = self.store.list_all(limit=999999)
        self.assertGreaterEqual(len(all_mems), 100)

    def _make_memory(self, raw: str) -> Memory:
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext("a", "test", "s1"),
            payload=MemoryPayload(raw=raw),
            metadata=MemoryMetadata(MemoryType.WORKING, PrivacyLevel.PUBLIC),
        )


class TestPrivacyFilter(unittest.TestCase):
    def test_secret_redacted(self):
        pf = PrivacyFilter()
        result = pf.sanitize("password: abc123", PrivacyLevel.SECRET)
        self.assertEqual(result, "[REDACTED]")

    def test_email_masked(self):
        pf = PrivacyFilter()
        result = pf.sanitize("Contact alice@example.com", PrivacyLevel.PRIVATE)
        self.assertEqual(result, "Contact <EMAIL>")

    def test_openai_key_masked(self):
        pf = PrivacyFilter()
        key = "sk-abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKL"  # 48 chars after sk-
        result = pf.sanitize(f"API key: {key}", PrivacyLevel.PRIVATE)
        self.assertIn("<OPENAI_API_KEY>", result)
        self.assertNotIn(key, result)

    def test_uuid_not_masked(self):
        """Improved filter: UUID should NOT be masked as API key."""
        pf = PrivacyFilter()
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        result = pf.sanitize(f"ID: {uuid}", PrivacyLevel.PRIVATE)
        # UUID should remain intact (old filter would have masked it)
        self.assertIn(uuid, result)

    def test_chinese_phone_masked(self):
        pf = PrivacyFilter()
        result = pf.sanitize("Contact 13812345678", PrivacyLevel.PRIVATE)
        self.assertIn("<PHONE>", result)
        self.assertNotIn("13812345678", result)

    def test_bearer_token_masked(self):
        pf = PrivacyFilter()
        result = pf.sanitize("Authorization: Bearer abc123.def456.ghi789", PrivacyLevel.PRIVATE)
        self.assertIn("Bearer <TOKEN>", result)


class TestDeduplicationWindow(unittest.TestCase):
    def test_duplicate_detected(self):
        dw = DeduplicationWindow(window_seconds=300)
        p1 = MemoryPayload(raw="same")
        p2 = MemoryPayload(raw="same")
        self.assertFalse(dw.is_duplicate(p1))
        self.assertTrue(dw.is_duplicate(p2))

    def test_expired_window(self):
        dw = DeduplicationWindow(window_seconds=0.1)
        p = MemoryPayload(raw="same")
        self.assertFalse(dw.is_duplicate(p))
        time.sleep(0.2)
        self.assertFalse(dw.is_duplicate(p))

    def test_thread_safety(self):
        """Verify dedup window is thread-safe."""
        dw = DeduplicationWindow(window_seconds=60)
        errors = []

        def worker():
            try:
                for i in range(100):
                    p = MemoryPayload(raw=f"item_{i}")
                    dw.is_duplicate(p)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)


class TestForgettingEngine(unittest.TestCase):
    def test_working_forgets_fast(self):
        stores = {MemoryType.WORKING: InMemoryStore()}
        fe = ForgettingEngine(stores)
        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(created_at=time.time() - 7200),  # 2 hours old
            context=AgentContext("a", "test", "s1"),
            payload=MemoryPayload(raw="x"),
            metadata=MemoryMetadata(MemoryType.WORKING, PrivacyLevel.PUBLIC, importance=1.0),
        )
        self.assertTrue(fe.should_forget(mem))

    def test_semantic_persists(self):
        stores = {MemoryType.SEMANTIC: InMemoryStore()}
        fe = ForgettingEngine(stores)
        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(created_at=time.time() - 3600),  # 1 hour old
            context=AgentContext("a", "test", "s1"),
            payload=MemoryPayload(raw="x"),
            metadata=MemoryMetadata(MemoryType.SEMANTIC, PrivacyLevel.PUBLIC, importance=9.0),
        )
        self.assertFalse(fe.should_forget(mem))


class TestUniversalMemorySystem(unittest.TestCase):
    def setUp(self):
        self.ums = UniversalMemorySystem()

    def test_observe_and_recall(self):
        ctx = AgentContext(agent_id="a1", agent_type="test", session_id="s1")
        self.ums.observe(AgentEvent(
            event_type=EventType.USER_INPUT,
            agent_context=ctx,
            content="I love pizza",
            structured_data={"fact": "User loves pizza", "importance": 8.0, "category": "food"},
        ))
        self.ums.observe(AgentEvent(
            event_type=EventType.SESSION_END,
            agent_context=ctx,
            content="Session ended",
        ))

        ctx2 = AgentContext(agent_id="a1", agent_type="test", session_id="s2")
        results = self.ums.recall("pizza", context=ctx2, budget_tokens=1000)
        self.assertTrue(len(results) > 0)
        self.assertTrue(any("pizza" in m.payload.raw.lower() for m in results))

    def test_remember_explicit(self):
        ctx = AgentContext(agent_id="a1", agent_type="test", session_id="s1")
        mid = self.ums.remember("Sky is blue", ctx, importance=9.0, category="fact")
        self.assertIsInstance(mid, MemoryId)

        results = self.ums.recall("sky color", context=ctx, budget_tokens=1000)
        self.assertTrue(any("blue" in m.payload.raw for m in results))

    def test_forget(self):
        ctx = AgentContext(agent_id="a1", agent_type="test", session_id="s1")
        mid = self.ums.remember("Forget me", ctx)
        self.assertTrue(self.ums.forget(str(mid)))

        results = self.ums.recall("Forget me", context=ctx, budget_tokens=1000)
        self.assertEqual(len(results), 0)

    def test_stats(self):
        stats = self.ums.get_stats()
        self.assertIn("WORKING", stats)
        self.assertIn("EPISODIC", stats)
        self.assertIn("SEMANTIC", stats)
        self.assertIn("PROCEDURAL", stats)

    def test_graceful_degradation_on_embedding_failure(self):
        """If embedding function fails, recall should still work via keyword fallback."""
        def bad_embedding_fn(text):
            raise RuntimeError("embedding service down")

        ums = UniversalMemorySystem(embedding_fn=bad_embedding_fn)
        ctx = AgentContext(agent_id="a1", agent_type="test", session_id="s1")
        ums.remember("Kubernetes is a container orchestrator", ctx)
        results = ums.recall("Kubernetes orchestration", context=ctx, budget_tokens=1000)
        self.assertTrue(len(results) > 0, "Recall should fall back to keyword search when embedding fails")

    def test_thread_safety_observe(self):
        """Multiple threads observing simultaneously should not crash."""
        ums = UniversalMemorySystem()
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    ctx = AgentContext(agent_id=f"agent_{n}", agent_type="test", session_id=f"sess_{n}_{i}")
                    ums.observe(AgentEvent(
                        event_type=EventType.USER_INPUT,
                        agent_context=ctx,
                        content=f"Message {i} from worker {n}",
                    ))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent observe errors: {errors}")

    def test_config_from_env(self):
        """Configuration should be loadable from environment variables."""
        os.environ["UAMS_WORKING_TTL"] = "600"
        os.environ["UAMS_RRF_K"] = "30"
        config = UAMSConfig.from_env()
        self.assertEqual(config.working_ttl_seconds, 600.0)
        self.assertEqual(config.rrf_k, 30)
        # Clean up
        del os.environ["UAMS_WORKING_TTL"]
        del os.environ["UAMS_RRF_K"]


class TestSQLiteStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = SQLiteStore(self.db_path, "test")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_store_and_retrieve(self):
        mem = self._make_memory("SQLite test memory")
        self.store.store(mem)
        fetched = self.store.retrieve(str(mem.id))
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.payload.raw, "SQLite test memory")

    def test_persistence(self):
        """Data should survive store re-initialization."""
        mem = self._make_memory("persistent data")
        self.store.store(mem)
        mid = str(mem.id)

        # Re-open the database
        store2 = SQLiteStore(self.db_path, "test")
        fetched = store2.retrieve(mid)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.payload.raw, "persistent data")

    def test_delete_expired(self):
        mem = self._make_memory("expired")
        mem.anchor.expires_at = time.time() - 1
        self.store.store(mem)
        self.assertEqual(self.store.delete_expired(), 1)
        self.assertIsNone(self.store.retrieve(str(mem.id)))

    def test_list_all(self):
        for i in range(5):
            self.store.store(self._make_memory(f"item {i}"))
        results = self.store.list_all(limit=10)
        self.assertEqual(len(results), 5)

    def _make_memory(self, raw: str) -> Memory:
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext("a", "test", "s1"),
            payload=MemoryPayload(raw=raw),
            metadata=MemoryMetadata(MemoryType.WORKING, PrivacyLevel.PUBLIC),
        )


class TestSerializationRoundTrip(unittest.TestCase):
    def test_memory_to_json_and_back(self):
        mem = Memory(
            id=MemoryId("test-123"),
            anchor=TemporalAnchor(created_at=1000.0, accessed_at=2000.0),
            context=AgentContext("agent1", "assistant", "sess1", user_id="alice"),
            payload=MemoryPayload(raw="hello", structured={"key": "value"}),
            metadata=MemoryMetadata(
                memory_type=MemoryType.SEMANTIC,
                privacy=PrivacyLevel.PUBLIC,
                importance=8.0,
                tags={"tag1"},
                categories={"cat1"},
            ),
        )
        data = mem.to_json()
        self.assertEqual(data["id"], "test-123")
        self.assertEqual(data["context"]["user_id"], "alice")
        self.assertEqual(data["metadata"]["importance"], 8.0)

        restored = Memory.from_json(data)
        self.assertEqual(str(restored.id), "test-123")
        self.assertEqual(restored.payload.raw, "hello")
        self.assertEqual(restored.metadata.importance, 8.0)
        self.assertIn("tag1", restored.metadata.tags)


class TestMetricsCollector(unittest.TestCase):
    def test_counter_increment(self):
        m = MetricsCollector()
        m.inc("events", 5)
        m.inc("events", 3)
        output = m.render()
        self.assertIn("events 8", output)

    def test_histogram_observe(self):
        m = MetricsCollector()
        m.observe("latency", 0.1)
        m.observe("latency", 0.2)
        output = m.render()
        self.assertIn("latency_count 2", output)
        self.assertIn("latency_sum 0.3", output)


class TestTokenEstimator(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_english_text(self):
        tokens = estimate_tokens("Hello world, this is a test.")
        # Heuristic: ~4 chars per token for English
        self.assertGreater(tokens, 0)
        self.assertLess(tokens, 20)

    def test_chinese_text(self):
        tokens = estimate_tokens("你好世界，这是一个测试。")
        # Heuristic: ~1 token per CJK char
        self.assertGreaterEqual(tokens, 10)

    def test_mixed_text(self):
        tokens = estimate_tokens("Hello 世界，这是一个 test.")
        self.assertGreater(tokens, 5)


class TestIntegrationEndToEnd(unittest.TestCase):
    """End-to-end integration test: 3 sessions, cross-session recall, multi-agent."""

    def test_three_session_travel_agent(self):
        ums = UniversalMemorySystem()

        # Session 1: Alice shares preferences
        ctx1 = AgentContext(agent_id="pa_001", agent_type="personal_assistant", session_id="day1", user_id="alice")
        ums.observe(AgentEvent(
            event_type=EventType.USER_INPUT,
            agent_context=ctx1,
            content="I love sushi and I prefer quiet neighborhoods.",
            structured_data={"fact": "Alice loves sushi and quiet neighborhoods", "importance": 8.0, "category": "preference"},
        ))
        ums.observe(AgentEvent(event_type=EventType.SESSION_END, agent_context=ctx1, content="end"))

        # Session 2: Alice mentions a trip to Tokyo
        ctx2 = AgentContext(agent_id="pa_001", agent_type="personal_assistant", session_id="day2", user_id="alice")
        ums.observe(AgentEvent(
            event_type=EventType.USER_INPUT,
            agent_context=ctx2,
            content="I'm planning a trip to Tokyo next week.",
        ))
        ums.observe(AgentEvent(event_type=EventType.SESSION_END, agent_context=ctx2, content="end"))

        # Session 3: Alice asks for restaurant recommendations
        ctx3 = AgentContext(agent_id="pa_001", agent_type="personal_assistant", session_id="day3", user_id="alice")
        results = ums.recall("Tokyo restaurant sushi", context=ctx3, budget_tokens=1000)

        # Should recall at least one memory about sushi preferences
        self.assertTrue(len(results) > 0, "Should recall cross-session preferences")
        texts = " ".join([m.payload.raw for m in results])
        self.assertIn("sushi", texts.lower())

        # Verify stats are sane
        stats = ums.get_stats()
        self.assertGreaterEqual(stats["SEMANTIC"], 1)
        self.assertGreaterEqual(stats["EPISODIC"], 2)

    def test_multi_agent_signal_and_lease(self):
        ums = UniversalMemorySystem()
        ums.enable_multi_agent()

        # Agent A acquires lease
        self.assertTrue(ums.acquire_lock("agent_a", "dataset_001", ttl=60.0))

        # Agent B is blocked
        self.assertFalse(ums.acquire_lock("agent_b", "dataset_001", ttl=60.0))

        # Agent A signals Agent B
        from uams.multi_agent.coordinator import Signal
        ums.send_signal(Signal(sender="agent_a", recipient="agent_b", signal_type="data_ready", payload={"size": 1000}))

        # Agent B reads signal
        signals = ums.read_signals("agent_b")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].type, "data_ready")
        self.assertEqual(signals[0].payload["size"], 1000)

        # Agent A releases lease
        self.assertTrue(ums.release_lock("agent_a", "dataset_001"))

        # Now Agent B can acquire
        self.assertTrue(ums.acquire_lock("agent_b", "dataset_001", ttl=60.0))


class TestRedisStoreGracefulDegradation(unittest.TestCase):
    """RedisStore should not crash when redis is not available."""

    def test_no_redis_no_crash(self):
        import sys
        # Clean up if mock modules were injected by earlier tests
        if "redis" in sys.modules:
            del sys.modules["redis"]
        from uams.storage.redis import RedisStore
        store = RedisStore(host="nonexistent_host", port=12345)
        self.assertFalse(store._available)

        # All operations should return gracefully
        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw="test"),
            metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
        )
        store.store(mem)  # should not crash
        self.assertIsNone(store.retrieve(str(mem.id)))  # should not crash
        self.assertFalse(store.delete(str(mem.id)))  # should not crash
        self.assertEqual(store.search_keywords("test"), [])
        self.assertEqual(store.search_vector([0.1, 0.2]), [])
        self.assertEqual(store.search_graph("test"), [])
        self.assertEqual(store.list_all(), [])
        self.assertEqual(store.delete_expired(), 0)
        self.assertFalse(store.publish_signal("ch", {}))
        self.assertIsNone(store.subscribe_signals("ch"))


class TestNeo4jStoreGracefulDegradation(unittest.TestCase):
    """Neo4jStore should not crash when neo4j is not available."""

    def test_no_neo4j_no_crash(self):
        import sys
        # Clean up if mock modules were injected by earlier tests
        if "neo4j" in sys.modules:
            del sys.modules["neo4j"]
        from uams.storage.neo4j import Neo4jStore
        store = Neo4jStore(uri="bolt://nonexistent:7687", user="neo4j", password="wrong")
        self.assertFalse(store._available)

        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw="test"),
            metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
        )
        store.store(mem)  # should not crash
        self.assertIsNone(store.retrieve(str(mem.id)))  # should not crash
        self.assertFalse(store.delete(str(mem.id)))  # should not crash
        self.assertEqual(store.search_keywords("test"), [])
        self.assertEqual(store.search_vector([0.1, 0.2]), [])
        self.assertEqual(store.search_graph("test"), [])
        self.assertEqual(store.list_all(), [])
        self.assertEqual(store.delete_expired(), 0)
        self.assertEqual(store.get_related_memories(str(mem.id)), [])


class TestChromaDBStoreRetrieve(unittest.TestCase):
    """ChromaDB store should support full memory reconstruction when available."""

    def test_store_and_retrieve_roundtrip(self):
        from uams.storage.chromadb import ChromaDBStore
        store = ChromaDBStore(collection_name="test_retrieve")
        if not store._available:
            self.skipTest("chromadb not available")

        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(created_at=12345.0, expires_at=99999.0),
            context=AgentContext(
                agent_id="agent1", agent_type="test", session_id="sess1",
                user_id="u1", team_id="t1", project_id="p1"
            ),
            payload=MemoryPayload(raw="ChromaDB retrieve test", structured={"key": "val"}, embedding=[0.1, 0.2, 0.3]),
            metadata=MemoryMetadata(
                memory_type=MemoryType.SEMANTIC, privacy=PrivacyLevel.PRIVATE,
                importance=8.0, confidence=0.95, tags={"tag1"}, categories={"cat1"}
            ),
        )
        store.store(mem)
        retrieved = store.retrieve(str(mem.id))
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.payload.raw, "ChromaDB retrieve test")
        self.assertEqual(retrieved.context.agent_id, "agent1")
        self.assertEqual(retrieved.metadata.memory_type, MemoryType.SEMANTIC)
        self.assertEqual(retrieved.metadata.privacy, PrivacyLevel.PRIVATE)
        self.assertEqual(retrieved.metadata.importance, 8.0)
        self.assertEqual(retrieved.metadata.confidence, 0.95)
        self.assertIn("tag1", retrieved.metadata.tags)
        self.assertIn("cat1", retrieved.metadata.categories)
        self.assertEqual(retrieved.context.user_id, "u1")
        self.assertEqual(retrieved.context.team_id, "t1")
        self.assertEqual(retrieved.context.project_id, "p1")
        self.assertEqual(retrieved.anchor.created_at, 12345.0)

        # Clean up
        store.delete(str(mem.id))


class TestUAMSConfigExtended(unittest.TestCase):
    """Config should support all new backends."""

    def test_redis_and_neo4j_defaults(self):
        config = UAMSConfig()
        self.assertEqual(config.storage_backend, "memory")
        self.assertEqual(config.redis_host, "localhost")
        self.assertEqual(config.redis_port, 6379)
        self.assertEqual(config.redis_db, 0)
        self.assertEqual(config.redis_key_prefix, "uams:memory:")
        self.assertEqual(config.neo4j_uri, "bolt://localhost:7687")
        self.assertEqual(config.neo4j_user, "neo4j")
        self.assertEqual(config.neo4j_database, "neo4j")

    def test_env_override(self):
        os.environ["UAMS_REDIS_HOST"] = "redis.cluster.local"
        os.environ["UAMS_REDIS_PORT"] = "6380"
        os.environ["UAMS_NEO4J_URI"] = "bolt://neo4j.prod:7687"
        config = UAMSConfig.from_env()
        self.assertEqual(config.redis_host, "redis.cluster.local")
        self.assertEqual(config.redis_port, 6380)
        self.assertEqual(config.neo4j_uri, "bolt://neo4j.prod:7687")
        # Clean up
        del os.environ["UAMS_REDIS_HOST"]
        del os.environ["UAMS_REDIS_PORT"]
        del os.environ["UAMS_NEO4J_URI"]


if __name__ == "__main__":
    unittest.main()
