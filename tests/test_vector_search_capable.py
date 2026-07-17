"""Regression test for T06 (P1-2): vector_search_capable flag.

Pins:
- MemoryStore base has vector_search_capable=False by default
- InMemoryStore and ChromaDBStore set it to True
- Non-vector stores (SQLite/Redis/PostgreSQL/Neo4j) leave it at False
- Custom user-defined store defaults to False
"""

from __future__ import annotations

import unittest

from uams.storage.base import MemoryStore
from uams.storage.chromadb import ChromaDBStore
from uams.storage.memory import InMemoryStore
from uams.storage.neo4j import Neo4jStore
from uams.storage.postgresql import PostgreSQLStore
from uams.storage.redis import RedisStore
from uams.storage.sqlite import SQLiteStore


class TestVectorSearchCapable(unittest.TestCase):
    def test_base_default_is_false(self) -> None:
        self.assertFalse(MemoryStore.vector_search_capable)

    def test_in_memory_is_true(self) -> None:
        self.assertTrue(InMemoryStore.vector_search_capable)

    def test_chromadb_is_true(self) -> None:
        self.assertTrue(ChromaDBStore.vector_search_capable)

    def test_sqlite_is_false(self) -> None:
        self.assertFalse(SQLiteStore.vector_search_capable)

    def test_redis_is_false(self) -> None:
        self.assertFalse(RedisStore.vector_search_capable)

    def test_postgresql_is_false(self) -> None:
        self.assertFalse(PostgreSQLStore.vector_search_capable)

    def test_neo4j_is_false(self) -> None:
        self.assertFalse(Neo4jStore.vector_search_capable)

    def test_subclass_can_override(self) -> None:
        """A user-defined subclass that wants vector search must
        explicitly opt in by setting vector_search_capable = True.
        """
        class MyVectorStore(MemoryStore):
            vector_search_capable = True

            def store(self, memory): pass
            def retrieve(self, memory_id): pass
            def delete(self, memory_id): pass
            def search_keywords(self, query, k=10): return []
            def search_vector(self, vector, k=10, **filters): return []
            def search_graph(self, entity, depth=2): return []
            def list_all(self, limit=100): return []
            def delete_expired(self): return 0
            def close(self): pass
            def count(self): return 0

        self.assertTrue(MyVectorStore.vector_search_capable)

    def test_sqlite_search_vector_logs_info_fallback(self) -> None:
        """SQLiteStore.search_vector must log an INFO message so
        operators see the recency fallback in production logs.
        """
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = SQLiteStore(db_path=path, tier_name="test")
            try:
                with self.assertLogs("uams.uams.storage.sqlite", level="INFO") as cm:
                    store.search_vector([0.1, 0.2, 0.3], k=5)
                self.assertTrue(
                    any("recency-ordered" in m for m in cm.output),
                    msg=f"expected recency-ordered log, got {cm.output}",
                )
            finally:
                store.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                p = path + ext
                if os.path.exists(p):
                    os.unlink(p)


if __name__ == "__main__":
    unittest.main()