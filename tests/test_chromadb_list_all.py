"""Regression test for T10 (P1-6): ChromaDBStore.list_all() no
longer returns [].

Pins the v0.6.0 fix where the previously-stub list_all() now
walks the collection in 500-row batches via collection.get().
Without this fix, cascade in-edge discovery, delete_by_project_id,
and MigrationTool.migrate() all silently dropped every memory on
the ChromaDB backend — GDPR Article 17 deletion was a no-op.
"""

from __future__ import annotations

import unittest

from uams.core.enums import MemoryType, PrivacyLevel
from uams.core.models import (
    AgentContext,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    TemporalAnchor,
)
from uams.storage.chromadb import ChromaDBStore, _chroma_row_to_memory


def _make_mem(idx: int, *, project_id: str = "p") -> Memory:
    return Memory(
        id=MemoryId(f"m-{idx:04d}"),
        anchor=TemporalAnchor(),
        context=AgentContext(
            agent_id="a",
            agent_type="t",
            session_id="s",
            user_id="u",
            project_id=project_id,
        ),
        payload=MemoryPayload(raw=f"row {idx}", structured={}, embedding=None),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
        ),
    )


class TestChromaDBListAll(unittest.TestCase):
    """Uses chromadb's EphemeralClient if installed. Skips cleanly
    if chromadb is not on the test runner.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import chromadb  # noqa: F401
            cls._has_chromadb = True
        except ImportError:
            cls._has_chromadb = False

    def setUp(self) -> None:
        if not self._has_chromadb:
            self.skipTest("chromadb not installed")

    def test_list_all_returns_stored_memories(self) -> None:
        store = ChromaDBStore(collection_name=f"t1_{id(self)}")
        try:
            # Store 3 memories
            for i in range(3):
                store.store(_make_mem(i))
            # list_all must return them
            all_mems = store.list_all(limit=100)
            ids = sorted(str(m.id) for m in all_mems)
            self.assertEqual(ids, ["m-0000", "m-0001", "m-0002"])
        finally:
            try:
                store._client.reset()
            except Exception:
                pass

    def test_list_all_respects_limit(self) -> None:
        store = ChromaDBStore(collection_name=f"t2_{id(self)}")
        try:
            for i in range(5):
                store.store(_make_mem(i))
            limited = store.list_all(limit=2)
            self.assertEqual(len(limited), 2)
        finally:
            try:
                store._client.reset()
            except Exception:
                pass

    def test_list_all_on_empty_collection(self) -> None:
        store = ChromaDBStore(collection_name=f"t3_{id(self)}")
        try:
            self.assertEqual(store.list_all(limit=100), [])
        finally:
            try:
                store._client.reset()
            except Exception:
                pass

    def test_chroma_row_to_memory_helper(self) -> None:
        """The extracted helper preserves all fields."""
        meta = {
            "created_at": 1234.5,
            "accessed_at": None,
            "expires_at": None,
            "agent_id": "a",
            "agent_type": "t",
            "session_id": "s",
            "user_id": "u",
            "project_id": "p",
            "memory_type": "SEMANTIC",
            "privacy": "PUBLIC",
            "importance": 7.0,
            "confidence": 0.9,
            "tags": "",
            "categories": "",
        }
        m = _chroma_row_to_memory("m-1", meta, "raw text", embedding=[0.1, 0.2])
        self.assertEqual(m.context.project_id, "p")
        self.assertEqual(m.metadata.importance, 7.0)
        self.assertEqual(m.payload.embedding, [0.1, 0.2])


if __name__ == "__main__":
    unittest.main()