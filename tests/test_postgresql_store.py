"""Real-backend integration tests for PostgreSQLStore.

Connects to a live PostgreSQL server. The CI workflow provides one via
the ``postgres:15-alpine`` service container (see ``.github/workflows/ci.yml``).
Locally you can start one with docker or set ``UAMS_TEST_PG_HOST`` and
``UAMS_TEST_PG_PORT`` to point at an existing instance.

Each test uses a fresh table (``uams_test_<uuid>``) to avoid cross-test
interference. Tables are dropped in ``tearDown``.

Run:
    UAMS_TEST_PG_HOST=127.0.0.1 \\
    UAMS_TEST_PG_USER=postgres UAMS_TEST_PG_PASSWORD=postgres \\
    pytest tests/test_postgresql_store.py -v
"""

from __future__ import annotations

import os
import sys
import time
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _pg_available() -> bool:
    """True only if psycopg2 is installed AND a server is reachable."""
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return False
    host = os.environ.get("UAMS_TEST_PG_HOST", "127.0.0.1")
    port = int(os.environ.get("UAMS_TEST_PG_PORT", "5432"))
    user = os.environ.get("UAMS_TEST_PG_USER", "postgres")
    password = os.environ.get("UAMS_TEST_PG_PASSWORD", "postgres")
    dbname = os.environ.get("UAMS_TEST_PG_DB", "postgres")
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=port, user=user, password=password, dbname=dbname,
            connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:
        return False


def _pg_args():
    return dict(
        host=os.environ.get("UAMS_TEST_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("UAMS_TEST_PG_PORT", "5432")),
        database=os.environ.get("UAMS_TEST_PG_DB", "postgres"),
        user=os.environ.get("UAMS_TEST_PG_USER", "postgres"),
        password=os.environ.get("UAMS_TEST_PG_PASSWORD", "postgres"),
    )


def _make_memory(memory_id: str, raw: str = "hello pg", importance: float = 5.0):
    from uams.core.models import (
        Memory, MemoryId, TemporalAnchor, MemoryPayload, MemoryMetadata, AgentContext,
    )
    from uams.core.enums import MemoryType, PrivacyLevel
    return Memory(
        id=MemoryId(memory_id),
        anchor=TemporalAnchor(created_at=12345.0, expires_at=99999.0),
        context=AgentContext(
            agent_id="a1", agent_type="t", session_id="s1",
            user_id="u1", team_id="t1", project_id="p1",
        ),
        payload=MemoryPayload(
            raw=raw,
            structured={"source": "test", "tags_match": ["a", "b"]},
            embedding=[0.1, 0.2, 0.3, 0.4],
        ),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
            importance=importance, confidence=0.9,
            tags={"hello", "test"}, categories={"ci"},
        ),
    )


def _drop_table(table: str):
    args = _pg_args()
    try:
        import psycopg2
        conn = psycopg2.connect(**args, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            cur.execute("DROP TABLE IF EXISTS _schema_version CASCADE")
        conn.commit()
        conn.close()
    except Exception:
        pass


@unittest.skipUnless(_pg_available(), "PostgreSQL server not reachable (UAMS_TEST_PG_HOST)")
class TestPostgreSQLStoreInit(unittest.TestCase):
    def test_init_creates_schema_and_migration_table(self):
        from uams.storage.postgresql import PostgreSQLStore
        table = f"uams_test_init_{uuid.uuid4().hex[:8]}"
        store = PostgreSQLStore(table_name=table, **_pg_args())
        self.assertTrue(store._available, "PG init failed — env vars / connectivity wrong")

        args = _pg_args()
        import psycopg2
        conn = psycopg2.connect(**args, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                    (table,),
                )
                self.assertIsNotNone(cur.fetchone(),
                                     f"{table} should exist after init")
                cur.execute("SELECT 1 FROM _schema_version LIMIT 1")
                self.assertIsNotNone(cur.fetchone())
        finally:
            conn.close()
            store.close()
            _drop_table(table)


@unittest.skipUnless(_pg_available(), "PostgreSQL server not reachable")
class TestPostgreSQLStoreCRUD(unittest.TestCase):
    def setUp(self):
        from uams.storage.postgresql import PostgreSQLStore
        self.table = f"uams_test_crud_{uuid.uuid4().hex[:8]}"
        self.store = PostgreSQLStore(table_name=self.table, **_pg_args())
        if not self.store._available:
            self.skipTest("PostgreSQLStore._available is False")

    def tearDown(self):
        try:
            self.store.close()
        finally:
            _drop_table(self.table)

    def test_store_and_retrieve_roundtrip(self):
        mem = _make_memory("crud-1", raw="alice vegetarian")
        self.store.store(mem)
        got = self.store.retrieve("crud-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.payload.raw, "alice vegetarian")
        self.assertEqual(got.metadata.importance, 5.0)
        self.assertEqual(got.metadata.tags, {"hello", "test"})
        self.assertEqual(got.payload.structured["source"], "test")
        # embedding roundtrips via pickle — must deserialize to a list
        self.assertEqual(list(got.payload.embedding), [0.1, 0.2, 0.3, 0.4])

    def test_store_upsert_updates_existing_row(self):
        self.store.store(_make_memory("upsert-1", raw="v1"))
        self.store.store(_make_memory("upsert-1", raw="v2", importance=9.0))
        got = self.store.retrieve("upsert-1")
        self.assertEqual(got.payload.raw, "v2")
        self.assertEqual(got.metadata.importance, 9.0)

    def test_delete_existing_returns_true(self):
        self.store.store(_make_memory("del-1"))
        self.assertTrue(self.store.delete("del-1"))
        self.assertIsNone(self.store.retrieve("del-1"))

    def test_delete_missing_returns_false(self):
        # DB returns 0 rows affected -> False
        self.assertFalse(self.store.delete("not-there"))

    def test_retrieve_missing_returns_none(self):
        self.assertIsNone(self.store.retrieve("not-there"))


@unittest.skipUnless(_pg_available(), "PostgreSQL server not reachable")
class TestPostgreSQLStoreSearch(unittest.TestCase):
    def setUp(self):
        from uams.storage.postgresql import PostgreSQLStore
        self.table = f"uams_test_search_{uuid.uuid4().hex[:8]}"
        self.store = PostgreSQLStore(table_name=self.table, **_pg_args())
        if not self.store._available:
            self.skipTest("PostgreSQLStore._available is False")
        # Seed 3 memories with distinct text
        self.store.store(_make_memory("s-1", raw="alice loves vegetarian food"))
        self.store.store(_make_memory("s-2", raw="bob prefers meat recipes"))
        self.store.store(_make_memory("s-3", raw="carol enjoys gardening on weekends"))

    def tearDown(self):
        try:
            self.store.close()
        finally:
            _drop_table(self.table)

    def test_keyword_search_tsvector_match(self):
        results = self.store.search_keywords("vegetarian", k=5)
        ids = [str(m.id) for m in results]
        self.assertIn("s-1", ids)
        self.assertNotIn("s-2", ids)

    def test_vector_search_falls_back_to_recent(self):
        # PG without pgvector can't do real vector search; store delegates to recent
        results = self.store.search_vector([0.1, 0.2, 0.3], k=2)
        self.assertGreater(len(results), 0)

    def test_list_all_returns_seeded(self):
        results = self.store.list_all(limit=10)
        ids = {str(m.id) for m in results}
        self.assertEqual(ids, {"s-1", "s-2", "s-3"})


@unittest.skipUnless(_pg_available(), "PostgreSQL server not reachable")
class TestPostgreSQLStoreTTL(unittest.TestCase):
    def setUp(self):
        from uams.storage.postgresql import PostgreSQLStore
        from uams.core.models import Memory, MemoryId, TemporalAnchor, MemoryPayload, MemoryMetadata, AgentContext
        from uams.core.enums import MemoryType, PrivacyLevel
        self.table = f"uams_test_ttl_{uuid.uuid4().hex[:8]}"
        self.store = PostgreSQLStore(table_name=self.table, **_pg_args())
        if not self.store._available:
            self.skipTest("PostgreSQLStore._available is False")
        # One expired, one live
        mem_expired = Memory(
            id=MemoryId("expired"),
            anchor=TemporalAnchor(created_at=1.0, expires_at=2.0),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw="old"),
            metadata=MemoryMetadata(
                memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC,
            ),
        )
        # NOTE: 99999.0 in Unix-epoch seconds is 1970-01-02, well in the past,
        # which would falsely mark 'live' as expired. Use a far-future timestamp
        # (year ~5138) so the row genuinely survives delete_expired().
        mem_live = Memory(
            id=MemoryId("live"),
            anchor=TemporalAnchor(created_at=time.time(), expires_at=9_999_999_999.0),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw="fresh"),
            metadata=MemoryMetadata(
                memory_type=MemoryType.SEMANTIC, privacy=PrivacyLevel.PUBLIC,
            ),
        )
        self.store.store(mem_expired)
        self.store.store(mem_live)

    def tearDown(self):
        try:
            self.store.close()
        finally:
            _drop_table(self.table)

    def test_delete_expired_removes_only_expired(self):
        deleted = self.store.delete_expired()
        self.assertGreaterEqual(deleted, 1)
        # Both rows still exist by design (deleted count comes from rowcount,
        # which depends on the WHERE matching just the expired one)
        remaining = {str(m.id) for m in self.store.list_all(limit=10)}
        self.assertIn("live", remaining)
        self.assertNotIn("expired", remaining)


@unittest.skipUnless(_pg_available(), "PostgreSQL server not reachable")
class TestPostgreSQLStorePoolClose(unittest.TestCase):
    def test_close_is_idempotent(self):
        from uams.storage.postgresql import PostgreSQLStore
        table = f"uams_test_close_{uuid.uuid4().hex[:8]}"
        store = PostgreSQLStore(table_name=table, **_pg_args())
        if not store._available:
            self.skipTest("PostgreSQLStore._available is False")
        store.close()
        # Calling close again should not raise
        store.close()
        _drop_table(table)


if __name__ == "__main__":
    unittest.main()
