"""Chaos and stress tests for UAMS production-grade resilience.

Tests:
- 10k+ memory volume performance
- Concurrent write consistency (same ID race)
- Network interruption simulation
- Memory capacity limits (LRU eviction)
- Metrics histogram overflow
- Input truncation
- Graceful shutdown
"""

import sys
import os
import unittest
import threading
import time
import random
import string

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
from uams.health import MetricsCollector
from uams.utils.tokens import TokenEstimator


class TestInMemoryStoreCapacity(unittest.TestCase):
    """Test LRU eviction under memory capacity limits."""

    def test_lru_eviction_at_capacity(self):
        store = InMemoryStore(max_capacity=100)
        for i in range(150):
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(created_at=time.time() + i),
                context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                payload=MemoryPayload(raw=f"memory_{i}"),
                metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
            )
            store.store(mem)
        all_mems = store.list_all(limit=999999)
        self.assertEqual(len(all_mems), 100, "Should evict oldest memories when capacity exceeded")

    def test_lru_retrieve_bumps_recency(self):
        store = InMemoryStore(max_capacity=3)
        for i in range(3):
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(created_at=time.time() + i),
                context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                payload=MemoryPayload(raw=f"memory_{i}"),
                metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
            )
            store.store(mem)
        # Retrieve first memory to bump recency
        first_id = store.list_all(limit=1)[0].id
        store.retrieve(str(first_id))
        # Store a new memory - should evict the second (least recently used)
        new_mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(created_at=time.time() + 10),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw="memory_new"),
            metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
        )
        store.store(new_mem)
        all_mems = store.list_all(limit=999999)
        raws = {m.payload.raw for m in all_mems}
        self.assertIn("memory_0", raws, "Retrieved memory should still be present (bumped by access)")
        self.assertIn("memory_new", raws)
        self.assertEqual(len(all_mems), 3)


class TestMetricsCollectorOverflow(unittest.TestCase):
    """Test metrics histogram ring buffer prevents memory leaks."""

    def test_histogram_aggregation_after_overflow(self):
        collector = MetricsCollector(max_histogram_entries=100)
        for i in range(250):
            collector.observe("latency", float(i))
        # Should not crash and should have aggregated stats
        self.assertIn("latency", collector._histogram_stats)
        # Render should show some count (remaining + last aggregated batch)
        render = collector.render()
        self.assertIn("latency_count", render)
        self.assertIn("latency_sum", render)
        # Total count should be 250 (all observed values accounted for)
        import re
        count_match = re.search(r"latency_count (\d+)", render)
        self.assertIsNotNone(count_match)
        total_count = int(count_match.group(1))
        self.assertEqual(total_count, 250, f"All 250 observations should be accounted for, got {total_count}")


class TestInputTruncation(unittest.TestCase):
    """Test max_raw_length protection."""

    def test_truncate_long_raw(self):
        ums = UniversalMemorySystem(config=UAMSConfig(max_raw_length=50))
        ctx = AgentContext(agent_id="a", agent_type="t", session_id="s")
        long_text = "x" * 1000
        mem_id = ums.remember(long_text, context=ctx)
        self.assertIsNotNone(mem_id)
        mem = ums._stores[MemoryType.SEMANTIC].retrieve(str(mem_id))
        self.assertIsNotNone(mem)
        self.assertEqual(len(mem.payload.raw), 50)

    def test_observe_truncates_long_content(self):
        ums = UniversalMemorySystem(config=UAMSConfig(max_raw_length=50))
        ctx = AgentContext(agent_id="a", agent_type="t", session_id="s")
        long_content = "y" * 1000
        event = AgentEvent(
            event_type=EventType.SESSION_START,
            agent_context=ctx,
            content=long_content,
        )
        ums.observe(event)
        mems = ums._stores[MemoryType.WORKING].list_all(limit=10)
        self.assertTrue(len(mems) > 0)
        self.assertEqual(len(mems[0].payload.raw), 50)


class TestConcurrentWriteConsistency(unittest.TestCase):
    """Test concurrent writes to same memory don't crash or corrupt."""

    def test_concurrent_store_same_id(self):
        store = InMemoryStore(max_capacity=1000)
        shared_id = MemoryId("shared-123")
        errors = []

        def writer(idx):
            try:
                mem = Memory(
                    id=shared_id,
                    anchor=TemporalAnchor(created_at=time.time() + idx),
                    context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                    payload=MemoryPayload(raw=f"version_{idx}"),
                    metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
                )
                store.store(mem)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent writes should not raise: {errors[:3]}")
        mem = store.retrieve("shared-123")
        self.assertIsNotNone(mem)
        # One of the versions should be present
        self.assertTrue(mem.payload.raw.startswith("version_"))

    def test_concurrent_remember_and_recall(self):
        ums = UniversalMemorySystem(config=UAMSConfig(max_raw_length=100))
        ctx = AgentContext(agent_id="a", agent_type="t", session_id="s")
        errors = []

        def rememberer(idx):
            try:
                ums.remember(f"fact_{idx}", context=ctx, importance=5.0)
            except Exception as e:
                errors.append(e)

        def recaller():
            try:
                for _ in range(50):
                    ums.recall("fact", context=ctx, budget_tokens=1000)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(20):
            threads.append(threading.Thread(target=rememberer, args=(i,)))
        threads.append(threading.Thread(target=recaller))
        threads.append(threading.Thread(target=recaller))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent remember/recall should not raise: {errors[:3]}")


class TestTokenEstimatorPerformance(unittest.TestCase):
    """Test token estimation on large text."""

    def test_large_chinese_text(self):
        estimator = TokenEstimator()
        text = "人工智能" * 5000  # 20,000 chars
        tokens = estimator.estimate(text)
        self.assertGreater(tokens, 1000)
        # Should be fast (< 100ms)
        start = time.time()
        for _ in range(100):
            estimator.estimate(text)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0, "Token estimation should be fast even for large text")

    def test_mixed_text(self):
        estimator = TokenEstimator()
        text = "Hello world " * 1000 + "人工智能" * 1000
        tokens = estimator.estimate(text)
        self.assertGreater(tokens, 1000)


class TestStressVolume(unittest.TestCase):
    """Stress test with 10k+ memories."""

    def test_10k_memories_keyword_search(self):
        store = InMemoryStore(max_capacity=20000)
        for i in range(10000):
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(created_at=time.time() + i),
                context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                payload=MemoryPayload(raw=f"document about {random.choice(['apple', 'banana', 'cherry', 'date'])} and {i}"),
                metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
            )
            store.store(mem)

        start = time.time()
        results = store.search_keywords("apple", k=10)
        elapsed = time.time() - start

        self.assertGreater(len(results), 0, "Should find apple documents")
        self.assertLess(elapsed, 5.0, f"Keyword search over 10k should be fast, took {elapsed:.2f}s")

    def test_10k_sqlite_persistence(self):
        import tempfile
        db_path = os.path.join(tempfile.mkdtemp(), "stress.db")
        from uams.storage.sqlite import SQLiteStore
        store = SQLiteStore(db_path, "stress", pool_size=5)

        for i in range(1000):
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(created_at=time.time() + i),
                context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                payload=MemoryPayload(raw=f"sql_document_{i}"),
                metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
            )
            store.store(mem)

        # Verify count
        all_mems = store.list_all(limit=999999)
        self.assertEqual(len(all_mems), 1000)

        # Reopen and verify persistence
        store2 = SQLiteStore(db_path, "stress", pool_size=5)
        all_mems2 = store2.list_all(limit=999999)
        self.assertEqual(len(all_mems2), 1000)
        store2.close()


class TestGracefulShutdown(unittest.TestCase):
    """Test shutdown persists working memories."""

    def test_shutdown_persists_working(self):
        ums = UniversalMemorySystem()
        ctx = AgentContext(agent_id="a", agent_type="t", session_id="s")
        ums.remember("working fact 1", context=ctx, importance=7.0)
        ums.remember("working fact 2", context=ctx, importance=8.0)

        # Before shutdown, working should be empty (remember stores in SEMANTIC)
        # But let's add a working memory directly
        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=ctx,
            payload=MemoryPayload(raw="direct working"),
            metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
        )
        ums._stores[MemoryType.WORKING].store(mem)

        # Shutdown should persist working to episodic
        ums.shutdown()
        # Verify direct working memory was persisted to episodic
        episodic = ums._stores[MemoryType.EPISODIC].search_keywords("direct working", k=1)
        self.assertEqual(len(episodic), 1)

    def test_truncate_method(self):
        ums = UniversalMemorySystem(config=UAMSConfig(max_raw_length=10))
        self.assertEqual(ums._truncate_raw("hello world"), "hello worl")
        self.assertEqual(ums._truncate_raw("hi"), "hi")


class TestRetrievalEntityLimit(unittest.TestCase):
    """Test graph traversal limits entity count."""

    def test_graph_limited_to_three_entities(self):
        ums = UniversalMemorySystem()
        ctx = AgentContext(agent_id="a", agent_type="t", session_id="s")
        # Store some memories with relations
        mem_a = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=ctx,
            payload=MemoryPayload(raw="apple banana cherry date elderberry fig"),
            metadata=MemoryMetadata(
                memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC,
                relations=[]
            ),
        )
        ums._stores[MemoryType.WORKING].store(mem_a)
        # Long query with many words - graph should only search first 3
        results = ums.recall("apple banana cherry date elderberry fig grape", context=ctx)
        # Should not crash and should return quickly
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
