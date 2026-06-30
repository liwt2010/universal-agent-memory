"""Tests for A+ improvements: PostgreSQL, config validation, backup, security.

Note: PostgreSQL tests use mock (no real PostgreSQL server required).
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.config import UAMSConfig
from uams.utils.security import InputValidator, RateLimiter
from uams.utils.backup import BackupManager, MigrationTool
from uams.utils.retry import with_retry, RetryConfig, retry_call, global_retry_stats
from uams import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata, MemoryType, PrivacyLevel,
)
from uams.storage.memory import InMemoryStore


class TestUAMSConfigValidation(unittest.TestCase):
    """Config validation should catch invalid values."""

    def test_valid_config_passes(self):
        config = UAMSConfig()
        config.validate()  # should not raise

    def test_invalid_log_level(self):
        config = UAMSConfig(log_level="INVALID")
        with self.assertRaises(ValueError) as ctx:
            config.validate()
        self.assertIn("log_level", str(ctx.exception))

    def test_invalid_storage_backend(self):
        config = UAMSConfig(storage_backend="oracle")
        with self.assertRaises(ValueError) as ctx:
            config.validate()
        self.assertIn("storage_backend", str(ctx.exception))

    def test_negative_working_ttl(self):
        config = UAMSConfig(working_ttl_seconds=-1)
        with self.assertRaises(ValueError):
            config.validate()

    def test_port_out_of_range(self):
        config = UAMSConfig(health_check_port=70000)
        with self.assertRaises(ValueError):
            config.validate()

    def test_postgresql_pool_invalid(self):
        config = UAMSConfig(postgresql_pool_min=5, postgresql_pool_max=1)
        with self.assertRaises(ValueError):
            config.validate()

    def test_multiple_errors(self):
        config = UAMSConfig(
            event_bus_max_buffer=0,
            max_raw_length=0,
            memory_capacity=0,
        )
        with self.assertRaises(ValueError) as ctx:
            config.validate()
        err = str(ctx.exception)
        self.assertIn("event_bus_max_buffer", err)
        self.assertIn("max_raw_length", err)
        self.assertIn("memory_capacity", err)


class TestInputValidator(unittest.TestCase):
    """Security input validation tests."""

    def test_sql_injection_sanitized(self):
        malicious = "user' OR '1'='1'; DROP TABLE users; --"
        result = InputValidator.sanitize_sql(malicious)
        self.assertNotIn("DROP", result)
        self.assertNotIn(";", result)
        self.assertNotIn("OR", result)

    def test_xss_prevention(self):
        malicious = '<script>alert("xss")</script>'
        result = InputValidator.sanitize_html(malicious)
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;", result)

    def test_sanitize_all_combined(self):
        text = "Hello' OR 1=1; <script>alert(1)</script> " + "x" * 20000
        result = InputValidator.sanitize_all(text, max_length=100)
        self.assertLessEqual(len(result), 100)  # Length <= 100 after truncation
        self.assertNotIn("<script>", result)  # XSS removed
        # Note: after HTML escape, ';' may appear in entities like &lt; but not as SQL injection

    def test_empty_input(self):
        self.assertEqual(InputValidator.sanitize_sql(""), "")
        self.assertEqual(InputValidator.sanitize_html(""), "")


class TestRateLimiter(unittest.TestCase):
    """Rate limiting tests."""

    def test_rate_limit_allows_within_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60.0)
        for _ in range(5):
            self.assertTrue(limiter.is_allowed("key1"))

    def test_rate_limit_blocks_excess(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60.0)
        for _ in range(3):
            limiter.is_allowed("key2")
        self.assertFalse(limiter.is_allowed("key2"))

    def test_rate_limit_different_keys_independent(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60.0)
        self.assertTrue(limiter.is_allowed("key_a"))
        self.assertTrue(limiter.is_allowed("key_b"))

    def test_reset(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60.0)
        limiter.is_allowed("key3")
        self.assertFalse(limiter.is_allowed("key3"))
        limiter.reset("key3")
        self.assertTrue(limiter.is_allowed("key3"))


class TestBackupManager(unittest.TestCase):
    """Backup and restore tests."""

    def test_backup_and_restore_roundtrip(self):
        store = InMemoryStore(max_capacity=100)
        for i in range(10):
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(),
                context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                payload=MemoryPayload(raw=f"memory_{i}"),
                metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
            )
            store.store(mem)

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name

        manager = BackupManager(store)
        exported = manager.backup_to_file(path)
        self.assertEqual(exported, 10)

        # Restore to new store
        new_store = InMemoryStore(max_capacity=100)
        new_manager = BackupManager(new_store)
        imported = new_manager.restore_from_file(path)
        self.assertEqual(imported, 10)
        self.assertEqual(len(new_store.list_all(limit=100)), 10)

        os.unlink(path)

    def test_backup_to_dict(self):
        store = InMemoryStore(max_capacity=10)
        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw="test"),
            metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
        )
        store.store(mem)
        manager = BackupManager(store)
        data = manager.backup_to_dict()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["payload"]["raw"], "test")

    def test_restore_from_dict(self):
        store = InMemoryStore(max_capacity=10)
        manager = BackupManager(store)
        data = [{
            "id": "test-id",
            "anchor": {"created_at": 0},
            "context": {"agent_id": "a", "agent_type": "t", "session_id": "s"},
            "payload": {"raw": "restored"},
            "metadata": {"memory_type": "WORKING", "privacy": "PUBLIC", "importance": 5.0, "confidence": 1.0}
        }]
        imported = manager.restore_from_dict(data)
        self.assertEqual(imported, 1)


class TestMigrationTool(unittest.TestCase):
    """Migration between storage backends."""

    def test_migration_memory_to_memory(self):
        source = InMemoryStore(max_capacity=100)
        target = InMemoryStore(max_capacity=100)
        for i in range(5):
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(),
                context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                payload=MemoryPayload(raw=f"mem_{i}"),
                metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
            )
            source.store(mem)

        tool = MigrationTool()
        count = tool.migrate(source, target, batch_size=2)
        self.assertEqual(count, 5)
        self.assertEqual(len(target.list_all(limit=100)), 5)

    def test_migration_with_filter(self):
        source = InMemoryStore(max_capacity=100)
        target = InMemoryStore(max_capacity=100)
        for i in range(5):
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(),
                context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
                payload=MemoryPayload(raw=f"mem_{i}"),
                metadata=MemoryMetadata(
                    memory_type=MemoryType.WORKING,
                    privacy=PrivacyLevel.PUBLIC,
                    importance=8.0 if i % 2 == 0 else 3.0,
                ),
            )
            source.store(mem)

        tool = MigrationTool()
        count = tool.migrate_with_filter(
            source, target,
            filter_fn=lambda m: m.metadata.importance > 5.0,
            batch_size=10,
        )
        self.assertEqual(count, 3)  # indices 0, 2, 4 have importance 8.0


class TestRetryMechanism(unittest.TestCase):
    """Exponential backoff retry tests."""

    def test_retry_success_on_first_attempt(self):
        call_count = 0
        @with_retry(max_retries=2, base_delay=0.01)
        def success():
            nonlocal call_count
            call_count += 1
            return "ok"
        self.assertEqual(success(), "ok")
        self.assertEqual(call_count, 1)

    def test_retry_success_after_failures(self):
        call_count = 0
        @with_retry(max_retries=3, base_delay=0.01, jitter=False)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return "ok"
        self.assertEqual(flaky(), "ok")
        self.assertEqual(call_count, 3)

    def test_retry_exhausts_and_raises(self):
        call_count = 0
        @with_retry(max_retries=2, base_delay=0.01, jitter=False)
        def always_fail():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("always fail")
        with self.assertRaises(RuntimeError):
            always_fail()
        self.assertEqual(call_count, 3)  # initial + 2 retries

    def test_retry_config_custom_exceptions(self):
        config = RetryConfig(max_retries=1, base_delay=0.01, retryable_exceptions=(ValueError,))
        with self.assertRaises(ValueError):
            retry_call(lambda: (_ for _ in ()).throw(ValueError("test")), config)


class TestPostgreSQLStoreMock(unittest.TestCase):
    """PostgreSQLStore mock tests (no real server needed)."""

    def test_no_postgresql_no_crash(self):
        from uams.storage.postgresql import PostgreSQLStore
        store = PostgreSQLStore(host="nonexistent", port=12345, database="test", user="test", password="test")
        self.assertFalse(store._available)

        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw="test"),
            metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
        )
        store.store(mem)  # should not crash
        self.assertIsNone(store.retrieve(str(mem.id)))
        self.assertFalse(store.delete(str(mem.id)))
        self.assertEqual(store.search_keywords("test"), [])
        self.assertEqual(store.search_vector([0.1, 0.2]), [])
        self.assertEqual(store.search_graph("test"), [])
        self.assertEqual(store.list_all(), [])
        self.assertEqual(store.delete_expired(), 0)


class TestPostgreSQLStoreConfig(unittest.TestCase):
    """PostgreSQL configuration defaults."""

    def test_postgresql_defaults(self):
        config = UAMSConfig()
        self.assertEqual(config.postgresql_host, "localhost")
        self.assertEqual(config.postgresql_port, 5432)
        self.assertEqual(config.postgresql_database, "uams")
        self.assertEqual(config.postgresql_table, "uams_memories")
        self.assertEqual(config.postgresql_pool_min, 1)
        self.assertEqual(config.postgresql_pool_max, 10)


class TestBenchmarkSuite(unittest.TestCase):
    """Benchmark suite smoke tests."""

    def test_benchmark_store_small(self):
        from uams.benchmarks import BenchmarkSuite
        result = BenchmarkSuite.benchmark_store(n=100)
        self.assertGreaterEqual(result.ops_per_sec, 0)
        self.assertEqual(result.ops, 100)

    def test_benchmark_retrieve_small(self):
        from uams.benchmarks import BenchmarkSuite
        result = BenchmarkSuite.benchmark_retrieve(n=100)
        self.assertGreaterEqual(result.ops_per_sec, 0)

    def test_benchmark_search_keywords_small(self):
        from uams.benchmarks import BenchmarkSuite
        result = BenchmarkSuite.benchmark_search_keywords(n_memories=100, n_queries=10)
        self.assertGreaterEqual(result.ops_per_sec, 0)
        self.assertEqual(result.ops, 10)

    def test_benchmark_delete_expired(self):
        from uams.benchmarks import BenchmarkSuite
        result = BenchmarkSuite.benchmark_delete_expired(n=100)
        self.assertGreaterEqual(result.ops_per_sec, 0)
        self.assertEqual(result.details["deleted"], 50)  # half are expired

    def test_run_all_smoke(self):
        from uams.benchmarks import BenchmarkSuite
        results = BenchmarkSuite.run_all(n=50)
        self.assertEqual(len(results), 4)
        for r in results:
            self.assertGreaterEqual(r.ops_per_sec, 0)


if __name__ == "__main__":
    unittest.main()
