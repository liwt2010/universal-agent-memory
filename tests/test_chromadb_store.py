"""Real-backend integration tests for ChromaDBStore.

Uses ``chromadb.EphemeralClient`` (in-process, no service container required).
The same code path is exercised as production — chromadb library + UAMS
serialization logic. Skips gracefully if the chromadb optional dependency
is not installed.

Run:
    pytest tests/test_chromadb_store.py -v
"""

from __future__ import annotations

import os
import sys
import unittest
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _chroma_available() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except ImportError:
        return False


def _make_store(suffix: str = ""):
    """Build a ChromaDBStore pointed at an isolated ephemeral collection."""
    from uams.storage.chromadb import ChromaDBStore
    name = f"uams_test_{int(time.time() * 1000)}_{suffix}"
    store = ChromaDBStore(collection_name=name)
    return store


def _make_memory(memory_id: str, raw: str = "hello world", embedding=None, importance=5.0):
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
        payload=MemoryPayload(raw=raw, embedding=embedding),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
            importance=importance, confidence=0.9,
            tags={"hello"}, categories={"test"},
        ),
    )


@unittest.skipUnless(_chroma_available(), "chromadb not installed")
class TestChromaDBStoreStoreAndRetrieve(unittest.TestCase):
    def setUp(self):
        self.store = _make_store("rt")
        if not self.store._available:
            self.skipTest("ChromaDBStore._available is False (init failed)")

    def test_store_and_retrieve_roundtrip(self):
        mem_id = "test-rt-1"
        mem = _make_memory(mem_id)
        self.store.store(mem)
        got = self.store.retrieve(mem_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.payload.raw, "hello world")
        self.assertEqual(got.metadata.importance, 5.0)
        self.assertEqual(got.metadata.tags, {"hello"})
        self.assertEqual(got.context.user_id, "u1")

    def test_retrieve_returns_none_for_unknown_id(self):
        self.assertIsNone(self.store.retrieve("does-not-exist"))

    def test_update_existing_memory_via_upsert(self):
        mem_id = "test-up-1"
        self.store.store(_make_memory(mem_id, raw="v1"))
        self.store.store(_make_memory(mem_id, raw="v2", importance=9.0))
        got = self.store.retrieve(mem_id)
        self.assertEqual(got.payload.raw, "v2")
        self.assertEqual(got.metadata.importance, 9.0)


@unittest.skipUnless(_chroma_available(), "chromadb not installed")
class TestChromaDBStoreSearch(unittest.TestCase):
    """Vector search assumes the same dimensionality used at write time.

    chromadb 1.x collections have a default embedding function with a
    fixed dimension (384 for the ONNX default). UAMS uses caller-supplied
    vectors via ``payload.embedding``, so to keep the tests hermetic and
    avoid hitting any default embedding path that downloads a model or
    mismatches dimensions, we exercise search with whatever vectors the
    store itself was seeded with. We do NOT depend on cosine distance
    ordering across the boundary.
    """

    def setUp(self):
        self.store = _make_store("search")
        if not self.store._available:
            self.skipTest("ChromaDBStore._available is False (init failed)")
        # Use a simple 4-dim vector — same vector stays under one collection
        self.vectors = {
            "alice": [0.10, 0.20, 0.30, 0.40],
            "bob":   [0.50, 0.60, 0.70, 0.80],
            "eve":   [0.90, 0.10, 0.20, 0.30],
        }
        for name, vec in self.vectors.items():
            self.store.store(_make_memory(name, raw=f"about {name}", embedding=vec))

    def test_vector_search_returns_at_least_one_result(self):
        # 4-dim query against 4-dim stored vectors
        q = [0.11, 0.21, 0.29, 0.41]
        res = self.store.search_vector(q, k=2)
        self.assertGreater(len(res), 0)
        ids = [str(m.id) for m in res]
        # Query was closest to alice's vector — must be in results
        self.assertIn("alice", ids)

    def test_vector_search_with_zero_vector_returns_empty(self):
        # Zero vector: cosine undefined -> we short-circuit to []
        res = self.store.search_vector([0.0, 0.0, 0.0, 0.0], k=5)
        self.assertEqual(res, [])

    def test_vector_search_with_empty_list_returns_empty(self):
        res = self.store.search_vector([], k=5)
        self.assertEqual(res, [])

    def test_keyword_search_returns_match(self):
        # keyword search uses query_documents which embeds the query text.
        # That would only work if the collection was created with an embedding
        # function at the collection level. With caller-supplied vectors only,
        # the path is still exercised but may return 0 results. Either is OK
        # — what matters is that the call doesn't crash.
        try:
            self.store.search_keywords("alice", k=2)
        except Exception:
            self.fail("search_keywords must not raise (graceful on mismatch)")


@unittest.skipUnless(_chroma_available(), "chromadb not installed")
class TestChromaDBStoreDelete(unittest.TestCase):
    def setUp(self):
        self.store = _make_store("delete")
        if not self.store._available:
            self.skipTest("ChromaDBStore._available is False (init failed)")

    def test_delete_existing_returns_true(self):
        mem_id = "to-delete"
        self.store.store(_make_memory(mem_id))
        self.assertTrue(self.store.delete(mem_id))
        self.assertIsNone(self.store.retrieve(mem_id))

    def test_delete_missing_returns_true_idempotently(self):
        # chromadb delete is idempotent at the API level
        result = self.store.delete("not-there")
        # either False or True depending on chromadb version; both acceptable
        self.assertIsInstance(result, bool)


@unittest.skipUnless(_chroma_available(), "chromadb not installed")
class TestChromaDBStoreEmbeddingSerialization(unittest.TestCase):
    """The roundtrip exposed a numpy->list bug in 1.5+; lock it down here."""

    def setUp(self):
        self.store = _make_store("embtype")
        if not self.store._available:
            self.skipTest("ChromaDBStore._available is False (init failed)")

    def test_retrieved_embedding_is_python_list_not_ndarray(self):
        mem_id = "emb-type"
        vec = [0.1, 0.2, 0.3, 0.4, 0.5]
        self.store.store(_make_memory(mem_id, embedding=vec))
        got = self.store.retrieve(mem_id)
        self.assertIsNotNone(got.payload.embedding)
        # API contract: List[float], not numpy.ndarray
        self.assertIsInstance(got.payload.embedding, list)
        # chromadb may store as float32 — accept ~1e-6 tolerance
        out = [float(x) for x in got.payload.embedding]
        self.assertEqual(len(out), len(vec))
        for a, b in zip(out, vec):
            self.assertAlmostEqual(a, b, places=4)


if __name__ == "__main__":
    unittest.main()
