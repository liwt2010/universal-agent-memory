"""Real-backend integration tests for Neo4jStore.

Connects to a live Neo4j server. CI workflow provides one via the
``neo4j:5-community`` service container (see ``.github/workflows/ci.yml``),
which requires ``NEO4J_ACCEPT_LICENSE_AGREEMENT=yes`` in the workflow.
Locally you can start one with docker or set the UAMS_TEST_NEO4J_*
env vars to point at an existing instance.

Each test creates its own per-test Neo4j database (via CREATE DATABASE
+ START DATABASE) for full isolation, then wipes all nodes on tearDown.
Falls back to the default 'neo4j' database if CREATE DATABASE is denied.

Run:
    UAMS_TEST_NEO4J_URI=bolt://127.0.0.1:7687 \\
    UAMS_TEST_NEO4J_USER=neo4j UAMS_TEST_NEO4J_PASSWORD=testpass \\
    pytest tests/test_neo4j_store_real.py -v
"""

import os
import sys
import time
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, MemoryPayload, MemoryMetadata,
    AgentContext, Relation,
)
from uams.core.enums import MemoryType, PrivacyLevel


def _neo4j_available() -> bool:
    try:
        import neo4j  # noqa: F401
    except ImportError:
        return False
    # Guard against the legacy mock-injection in tests/test_neo4j_store.py
    # which sets ``sys.modules["neo4j"] = MagicMock()``. A MagicMock object
    # has no ``__file__`` attribute (real installed modules do), and it
    # also lacks the private ``_graph_database`` submodule that the real
    # neo4j driver exposes.
    if not getattr(neo4j, "__file__", None):
        return False
    try:
        import importlib
        importlib.import_module("neo4j._graph_database")
    except Exception:
        return False
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            os.environ.get("UAMS_TEST_NEO4J_URI", "bolt://127.0.0.1:7687"),
            auth=(
                os.environ.get("UAMS_TEST_NEO4J_USER", "neo4j"),
                os.environ.get("UAMS_TEST_NEO4J_PASSWORD", "password"),
            ),
            connection_timeout=5,
        )
        with driver.session(database="neo4j") as session:
            session.run("RETURN 1")
        driver.close()
        return True
    except Exception:
        return False


def _neo4j_args() -> dict:
    return dict(
        uri=os.environ.get("UAMS_TEST_NEO4J_URI", "bolt://127.0.0.1:7687"),
        user=os.environ.get("UAMS_TEST_NEO4J_USER", "neo4j"),
        password=os.environ.get("UAMS_TEST_NEO4J_PASSWORD", "password"),
    )


def _make_mem(memory_id: str, raw: str, relations=None, importance: float = 5.0):
    return Memory(
        id=MemoryId(memory_id),
        anchor=TemporalAnchor(created_at=12345.0),
        context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
        payload=MemoryPayload(raw=raw),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
            importance=importance, confidence=0.95,
            relations=[Relation(r["type"], r["target"], strength=r.get("strength", 1.0))
                       for r in (relations or [])],
        ),
    )


def _provision_db(args: dict, db_name: str) -> bool:
    """Try to create a per-test database; return False if RBAC forbids.

    Neo4j 5+ requires CREATE DATABASE against the 'system' database.
    Some deployments disallow it; tests then fall back to the default
    'neo4j' database and rely on per-test wipe.
    """
    try:
        from neo4j import GraphDatabase
        sys_driver = GraphDatabase.driver(
            args["uri"], auth=(args["user"], args["password"])
        )
        with sys_driver.session(database="system") as session:
            session.run(f"CREATE DATABASE {db_name} IF NOT EXISTS")
            for _ in range(20):
                try:
                    session.run(f"START DATABASE {db_name}")
                    break
                except Exception:
                    time.sleep(0.25)
                    continue
        sys_driver.close()
        return True
    except Exception:
        return False


def _wipe_all(store) -> None:
    """Delete every node and relationship in the test's database."""
    if not hasattr(store, "_driver") or store._driver is None:
        return
    try:
        with store._driver.session(database=store._database) as session:
            session.run("MATCH (n) DETACH DELETE n")
    except Exception:
        pass


@unittest.skipUnless(_neo4j_available(), "Neo4j server not reachable (UAMS_TEST_NEO4J_URI)")
class _Neo4jTestBase(unittest.TestCase):
    """Base class shared by Neo4j CRUD / Search / Graph tests.

    setUp is per-test (not setUpClass) so unreachable servers skip each
    test method individually instead of poisoning the whole class as
    errors via setUpClass failure.
    """
    """Common setup: try to claim a per-test Neo4j database, fall back to 'neo4j'."""

    def setUp(self):
        from uams.storage.neo4j import Neo4jStore
        args = _neo4j_args()
        # Try to claim a per-test database; fall back to 'neo4j' if the
        # cluster doesn't allow CREATE DATABASE (e.g. read-only perms).
        per_test_db = f"uamstest{uuid.uuid4().hex[:8]}"
        if _provision_db(args, per_test_db):
            self._per_test_db = per_test_db
            db_to_use = per_test_db
        else:
            self._per_test_db = None
            db_to_use = "neo4j"
        self.store = Neo4jStore(database=db_to_use, **args)
        if not self.store._available:
            self.skipTest("Neo4jStore._available is False")
        _wipe_all(self.store)

    def tearDown(self):
        try:
            _wipe_all(self.store)
        finally:
            self.store.close()


class TestNeo4jStoreCRUD(_Neo4jTestBase):

    def test_store_creates_node_and_roundtrips(self):
        mem = _make_mem("n-1", "alice vegetarian")
        self.store.store(mem)
        got = self.store.retrieve("n-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.payload.raw, "alice vegetarian")
        self.assertEqual(got.metadata.importance, 5.0)
        self.assertEqual(got.payload.embedding, [0.1, 0.2, 0.3, 0.4])

    def test_store_upsert_overrides_existing(self):
        self.store.store(_make_mem("upsert-1", "v1"))
        self.store.store(_make_mem("upsert-1", "v2", importance=9.0))
        got = self.store.retrieve("upsert-1")
        self.assertEqual(got.payload.raw, "v2")
        self.assertEqual(got.metadata.importance, 9.0)

    def test_retrieve_missing_returns_none(self):
        self.assertIsNone(self.store.retrieve("not-there"))

    def test_delete_existing_returns_true(self):
        self.store.store(_make_mem("del-1", "x"))
        self.assertTrue(self.store.delete("del-1"))
        self.assertIsNone(self.store.retrieve("del-1"))

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.store.delete("not-there"))

    def test_list_all_returns_seeded(self):
        self.store.store(_make_mem("s-1", "a"))
        self.store.store(_make_mem("s-2", "b"))
        self.store.store(_make_mem("s-3", "c"))
        results = self.store.list_all(limit=10)
        ids = {str(m.id) for m in results}
        self.assertSetEqual(ids, {"s-1", "s-2", "s-3"})


class TestNeo4jStoreSearch(_Neo4jTestBase):
    """Keyword / vector search against real Cypher queries."""

    def setUp(self):
        super().setUp()
        for label, raw in (("alice", "alice loves vegetarian food"),
                          ("bob", "bob prefers meat recipes"),
                          ("carol", "carol enjoys gardening on weekends")):
            self.store.store(_make_mem(label, raw))

    def test_keyword_search_finds_vegetarian(self):
        results = self.store.search_keywords("vegetarian", k=5)
        ids = [str(m.id) for m in results]
        self.assertIn("alice", ids)
        self.assertNotIn("bob", ids)

    def test_vector_search_falls_back_to_recent(self):
        results = self.store.search_vector([0.1, 0.2, 0.3], k=2)
        self.assertGreater(len(results), 0)


class TestNeo4jStoreGraph(_Neo4jTestBase):
    """Real graph traversal via Cypher patterns — the killer feature."""

    def setUp(self):
        super().setUp()
        # Build a 3-node chain where relations flow FORWARD (a -> b -> c).
        # search_graph's BFS follows outgoing RELATES edges, so this
        # layout makes the walk deterministic.
        self.store.store(_make_mem("node-a", "node a",
                                   relations=[
                                       {"type": "follows", "target": "node-b"},
                                       {"type": "follows", "target": "node-c"},
                                   ]))
        self.store.store(_make_mem("node-b", "node b",
                                   relations=[{"type": "follows", "target": "node-c"}]))
        self.store.store(_make_mem("node-c", "node c"))

    def test_search_graph_walks_relations(self):
        results = self.store.search_graph("node-a", depth=5)
        ids = {str(m.id) for m in results}
        # BFS from 'node-a' walks its outgoing 'follows' edges to b and c,
        # then from b to c again (already visited). All three nodes
        # must end up in the result set.
        self.assertSetEqual(ids, {"node-a", "node-b", "node-c"})


@unittest.skipUnless(_neo4j_available(), "Neo4j server not reachable")
class TestNeo4jStoreClose(unittest.TestCase):
    def test_close_is_idempotent(self):
        from uams.storage.neo4j import Neo4jStore
        store = Neo4jStore(database="neo4j", **_neo4j_args())
        if not store._available:
            self.skipTest("Neo4jStore._available is False")
        store.close()
        store.close()


if __name__ == "__main__":
    unittest.main()
