"""Mock-based unit tests for Neo4jStore.

Tests Neo4jStore logic without requiring a real Neo4j server.
Uses unittest.mock to simulate neo4j.GraphDatabase.driver behavior.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Inject mock neo4j module before importing Neo4jStore
_mock_neo4j = MagicMock()
_mock_neo4j.GraphDatabase = MagicMock()
sys.modules["neo4j"] = _mock_neo4j

from uams.storage.neo4j import Neo4jStore
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata, Relation,
)
from uams.core.enums import MemoryType, PrivacyLevel


class FakeRecord:
    """Fake Neo4j record that supports dict-style and attribute access."""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()


class FakeResult:
    """Fake Neo4j result with list of records."""

    def __init__(self, records):
        self._records = records

    def __iter__(self):
        return iter(self._records)

    def single(self):
        return self._records[0] if self._records else None


class FakeSession:
    """In-memory fake Neo4j session for testing."""

    def __init__(self):
        self._nodes: dict = {}  # id -> node properties
        self._relations: list = []  # list of (from_id, to_id, type, strength)
        self._counters = {"created": 0, "deleted": 0}

    def run(self, query, **parameters):
        query_lower = query.lower()

        # MERGE / SET (store)
        if "merge (m:memory" in query_lower and "set m +=" in query_lower:
            node_id = parameters.get("id")
            props = parameters.get("props", {})
            self._nodes[node_id] = props
            self._counters["created"] += 1
            return FakeResult([])

        # MATCH RETURN m (retrieve)
        if "match (m:memory {id: $id})" in query_lower and "return m" in query_lower:
            node_id = parameters.get("id")
            node = self._nodes.get(node_id)
            if node:
                return FakeResult([FakeRecord({"m": node})])
            return FakeResult([])

        # MATCH SET accessed_at
        if "match (m:memory {id: $id})" in query_lower and "set m.accessed_at" in query_lower:
            return FakeResult([])

        # Delete expired (must be checked before generic DELETE with OPTIONAL MATCH)
        if "delete r, m" in query_lower and "where m.expires_at" in query_lower:
            now = parameters.get("now", float("inf"))
            count = 0
            for node_id in list(self._nodes.keys()):
                expires = self._nodes[node_id].get("expires_at", 0)
                if expires > 0 and expires < now:
                    del self._nodes[node_id]
                    count += 1
            return FakeResult([FakeRecord({"deleted": count})])

        # DELETE with OPTIONAL MATCH (single ID)
        if "delete r, m" in query_lower:
            node_id = parameters.get("id")
            if node_id in self._nodes:
                del self._nodes[node_id]
                self._counters["deleted"] += 1
                return FakeResult([FakeRecord({"deleted": 1})])
            return FakeResult([FakeRecord({"deleted": 0})])

        # Fulltext search
        if "db.index.fulltext.querynodes" in query_lower:
            return FakeResult([])  # Not implemented in fake

        # CONTAINS keyword search
        if "where m.raw contains" in query_lower:
            term = parameters.get("term", "")
            results = []
            for node_id, node in self._nodes.items():
                raw = node.get("raw", "")
                if term.lower() in raw.lower():
                    results.append(FakeRecord({"m": node}))
            return FakeResult(results)

        # ORDER BY created_at DESC
        if "order by m.created_at desc" in query_lower:
            sorted_nodes = sorted(self._nodes.values(), key=lambda n: n.get("created_at", 0), reverse=True)
            limit = parameters.get("limit", 100)
            records = [FakeRecord({"m": n}) for n in sorted_nodes[:limit]]
            return FakeResult(records)

        # Graph traversal for search_graph (returns related_id)
        if "match (m:memory {id: $id})-[r:relates]->(related:memory)" in query_lower and "return related.id" in query_lower:
            node_id = parameters.get("id")
            records = []
            for rel in self._relations:
                if rel[0] == node_id:
                    records.append(FakeRecord({"related_id": rel[1], "rel_type": rel[2], "strength": rel[3]}))
            return FakeResult(records)

        # Graph traversal for get_related_memories (returns related as m)
        if "match (m:memory {id: $id})-[r:relates]->(related:memory)" in query_lower:
            node_id = parameters.get("id")
            records = []
            for rel in self._relations:
                if rel[0] == node_id:
                    target = self._nodes.get(rel[1])
                    if target:
                        records.append(FakeRecord({"m": target, "rel_type": rel[2], "strength": rel[3]}))
            return FakeResult(records)

        # WHERE raw contains entity
        if "where m.raw contains $entity" in query_lower:
            entity = parameters.get("entity", "")
            results = []
            for node_id, node in self._nodes.items():
                raw = node.get("raw", "")
                if entity.lower() in raw.lower():
                    results.append(FakeRecord({"m": node}))
            return FakeResult(results)

        # Delete expired
        if "delete r, m" in query_lower and "where m.expires_at" in query_lower:
            now = parameters.get("now", float("inf"))
            count = 0
            for node_id in list(self._nodes.keys()):
                expires = self._nodes[node_id].get("expires_at", 0)
                if expires > 0 and expires < now:
                    del self._nodes[node_id]
                    count += 1
            return FakeResult([FakeRecord({"deleted": count})])

        # RETURN count(m) as deleted
        if "return count(m) as deleted" in query_lower:
            return FakeResult([FakeRecord({"deleted": self._counters["deleted"]})])

        # RETURN 1 (ping test)
        if "return 1" in query_lower:
            return FakeResult([FakeRecord({"1": 1})])

        # Schema creation (constraints, indexes)
        if "create constraint" in query_lower or "create index" in query_lower or "create fulltext index" in query_lower:
            return FakeResult([])

        # Generic MERGE for agent/session relationships
        if "merge" in query_lower:
            return FakeResult([])

        return FakeResult([])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeDriver:
    """Fake Neo4j driver."""

    def __init__(self):
        self._session = FakeSession()

    def session(self, database=None):
        return self._session

    def close(self):
        pass


class TestNeo4jStoreMock(unittest.TestCase):

    def setUp(self):
        self.fake_driver = FakeDriver()
        # Ensure neo4j mock module is available for Neo4jStore.__init__
        if "neo4j" not in sys.modules:
            _mock_neo4j = MagicMock()
        else:
            _mock_neo4j = sys.modules["neo4j"]
        _mock_neo4j.GraphDatabase = MagicMock()
        _mock_neo4j.GraphDatabase.driver = lambda *args, **kwargs: self.fake_driver
        sys.modules["neo4j"] = _mock_neo4j
        self.store = Neo4jStore(uri="bolt://localhost:7687", user="neo4j", password="test")
        self.assertTrue(self.store._available)

    def tearDown(self):
        pass

    def _make_memory(self, raw="hello", mem_type=MemoryType.WORKING, tags=None, relations=None, importance=5.0):
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(created_at=1000.0),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw=raw),
            metadata=MemoryMetadata(
                memory_type=mem_type,
                privacy=PrivacyLevel.PUBLIC,
                importance=importance,
                tags=set(tags or []),
                relations=relations or [],
            ),
        )

    def test_store_and_retrieve(self):
        mem = self._make_memory(raw="neo4j test")
        self.store.store(mem)
        retrieved = self.store.retrieve(str(mem.id))
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.payload.raw, "neo4j test")
        self.assertEqual(retrieved.metadata.memory_type, MemoryType.WORKING)

    def test_delete(self):
        mem = self._make_memory(raw="delete me")
        self.store.store(mem)
        self.assertTrue(self.store.delete(str(mem.id)))
        self.assertIsNone(self.store.retrieve(str(mem.id)))
        self.assertFalse(self.store.delete("nonexistent"))

    def test_search_keywords(self):
        self.store.store(self._make_memory(raw="apple banana"))
        self.store.store(self._make_memory(raw="cherry date"))
        self.store.store(self._make_memory(raw="apple pie"))

        results = self.store.search_keywords("apple", k=10)
        self.assertEqual(len(results), 2)
        raws = {m.payload.raw for m in results}
        self.assertIn("apple banana", raws)
        self.assertIn("apple pie", raws)

    def test_search_graph(self):
        mem_a = self._make_memory(raw="node A", relations=[Relation("links", "target_b", 1.0)])
        mem_b = self._make_memory(raw="node B")
        self.store.store(mem_a)
        self.store.store(mem_b)

        # Manually add relation to fake session for traversal
        self.fake_driver._session._relations.append((str(mem_a.id), str(mem_b.id), "links", 1.0))

        results = self.store.search_graph("node A", depth=2)
        self.assertTrue(len(results) >= 1)
        raws = {m.payload.raw for m in results}
        self.assertIn("node A", raws)
        self.assertIn("node B", raws)

    def test_list_all(self):
        self.store.store(self._make_memory(raw="one"))
        self.store.store(self._make_memory(raw="two"))
        results = self.store.list_all(limit=10)
        self.assertTrue(len(results) >= 2)

    def test_delete_expired(self):
        import time
        expired = self._make_memory(raw="expired")
        fresh = self._make_memory(raw="fresh")
        self.store.store(expired)
        self.store.store(fresh)
        # Set expires_at in fake session
        self.fake_driver._session._nodes[str(expired.id)]["expires_at"] = time.time() - 100
        self.fake_driver._session._nodes[str(fresh.id)]["expires_at"] = time.time() + 10000
        count = self.store.delete_expired()
        self.assertGreaterEqual(count, 1)
        self.assertIsNone(self.store.retrieve(str(expired.id)))
        self.assertIsNotNone(self.store.retrieve(str(fresh.id)))

    def test_get_related_memories(self):
        mem_a = self._make_memory(raw="node A", relations=[Relation("links", "target_b", 1.0)])
        mem_b = self._make_memory(raw="node B")
        self.store.store(mem_a)
        self.store.store(mem_b)
        self.fake_driver._session._relations.append((str(mem_a.id), str(mem_b.id), "links", 1.0))

        related = self.store.get_related_memories(str(mem_a.id))
        self.assertTrue(len(related) >= 1)
        raws = {m.payload.raw for m in related}
        self.assertIn("node B", raws)

    def test_search_vector_fallback(self):
        """Vector search should fall back to recent memories."""
        self.store.store(self._make_memory(raw="vector test"))
        results = self.store.search_vector([0.1, 0.2], k=10)
        self.assertTrue(len(results) >= 1)


if __name__ == "__main__":
    unittest.main()
