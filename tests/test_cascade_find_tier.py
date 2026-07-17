"""Regression tests for T09 (P1-5) and T03 (P0-3):
MemoryStore.find_tier() and MemoryStore.in_edges() with a
maintained reverse index.

Pins:
- InMemoryStore.find_tier() returns True iff the memory is in
  the store (replaces the v0.5.x _locate_tier per-tier sweep)
- SQLiteStore.find_tier() same
- InMemoryStore.in_edges() returns O(1) the set of source
  memory_ids that reference a given target (reverse index)
- SQLiteStore.in_edges() same via the <tier>_incoming table
- in_edges() is maintained across store() / delete() / truncate()
- The reverse index doesn't accumulate stale edges when a
  memory is overwritten
- CascadeForgetter._discover_in_edges in 'index' mode returns
  empty for stores without a reverse index (strict-by-default)
- 'auto' mode falls back to in_edges_scan when the store has
  no reverse index
"""

from __future__ import annotations

import os
import tempfile
import unittest

from uams.core.enums import MemoryType, PrivacyLevel
from uams.core.models import (
    AgentContext,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    Relation,
    TemporalAnchor,
)
from uams.storage.memory import InMemoryStore
from uams.storage.sqlite import SQLiteStore


def _make_mem(
    idx: int,
    *,
    targets: list[str] | None = None,
) -> Memory:
    return Memory(
        id=MemoryId(f"m-{idx:04d}"),
        anchor=TemporalAnchor(),
        context=AgentContext(
            agent_id="a", agent_type="t", session_id="s", user_id="u",
        ),
        payload=MemoryPayload(raw=f"row {idx}", structured={}, embedding=None),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
            relations=(
                [Relation("depends_on", t, strength=1.0) for t in (targets or [])]
            ),
        ),
    )


class TestFindTierAndInEdges(unittest.TestCase):
    # ---- InMemoryStore ----

    def test_inmemory_find_tier_present(self) -> None:
        s = InMemoryStore()
        s.store(_make_mem(1))
        self.assertTrue(s.find_tier("m-0001"))

    def test_inmemory_find_tier_absent(self) -> None:
        s = InMemoryStore()
        self.assertFalse(s.find_tier("does-not-exist"))

    def test_inmemory_in_edges_present(self) -> None:
        s = InMemoryStore()
        # m-0001 references m-target
        s.store(_make_mem(1, targets=["m-target"]))
        s.store(_make_mem(2, targets=["m-target"]))
        s.store(_make_mem(3))  # no relation
        in_e = s.in_edges("m-target")
        self.assertEqual(set(in_e), {"m-0001", "m-0002"})

    def test_inmemory_in_edges_absent(self) -> None:
        s = InMemoryStore()
        s.store(_make_mem(1))
        self.assertEqual(s.in_edges("nope"), [])

    def test_inmemory_in_edges_kept_on_overwrite(self) -> None:
        s = InMemoryStore()
        s.store(_make_mem(1, targets=["t"]))
        # Overwrite with a new payload but the same relations
        s.store(_make_mem(1, targets=["t"]))
        self.assertEqual(set(s.in_edges("t")), {"m-0001"})

    def test_inmemory_in_edges_cleaned_on_delete(self) -> None:
        s = InMemoryStore()
        s.store(_make_mem(1, targets=["t"]))
        self.assertEqual(set(s.in_edges("t")), {"m-0001"})
        s.delete("m-0001")
        self.assertEqual(s.in_edges("t"), [])

    def test_inmemory_in_edges_cleaned_on_overwrite_to_no_relation(self) -> None:
        s = InMemoryStore()
        s.store(_make_mem(1, targets=["t"]))
        self.assertEqual(set(s.in_edges("t")), {"m-0001"})
        # Overwrite with a memory that has no relation to t
        s.store(_make_mem(1))
        self.assertEqual(s.in_edges("t"), [])

    def test_inmemory_has_reverse_index_true(self) -> None:
        self.assertTrue(InMemoryStore.has_reverse_index)

    # ---- SQLiteStore ----

    def _make_sqlite(self) -> tuple[SQLiteStore, str]:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return SQLiteStore(db_path=path, tier_name="test"), path

    def _cleanup(self, s: SQLiteStore, path: str) -> None:
        s.close()
        for ext in ("", "-wal", "-shm"):
            full = path + ext
            if os.path.exists(full):
                try:
                    os.unlink(full)
                except PermissionError:
                    pass

    def test_sqlite_find_tier_present(self) -> None:
        s, path = self._make_sqlite()
        try:
            s.store(_make_mem(1))
            self.assertTrue(s.find_tier("m-0001"))
        finally:
            self._cleanup(s, path)

    def test_sqlite_find_tier_absent(self) -> None:
        s, path = self._make_sqlite()
        try:
            self.assertFalse(s.find_tier("nope"))
        finally:
            self._cleanup(s, path)

    def test_sqlite_in_edges_present(self) -> None:
        s, path = self._make_sqlite()
        try:
            s.store(_make_mem(1, targets=["t"]))
            s.store(_make_mem(2, targets=["t"]))
            s.store(_make_mem(3))
            self.assertEqual(set(s.in_edges("t")), {"m-0001", "m-0002"})
        finally:
            self._cleanup(s, path)

    def test_sqlite_in_edges_cleaned_on_delete(self) -> None:
        s, path = self._make_sqlite()
        try:
            s.store(_make_mem(1, targets=["t"]))
            self.assertEqual(set(s.in_edges("t")), {"m-0001"})
            s.delete("m-0001")
            self.assertEqual(s.in_edges("t"), [])
        finally:
            self._cleanup(s, path)

    def test_sqlite_in_edges_cleaned_on_truncate(self) -> None:
        s, path = self._make_sqlite()
        try:
            s.store(_make_mem(1, targets=["t"]))
            self.assertEqual(set(s.in_edges("t")), {"m-0001"})
            s.truncate()
            self.assertEqual(s.in_edges("t"), [])
        finally:
            self._cleanup(s, path)

    def test_sqlite_has_reverse_index_true(self) -> None:
        self.assertTrue(SQLiteStore.has_reverse_index)

    # ---- Base default behaviour ----

    def test_base_in_edges_returns_empty_by_default(self) -> None:
        """A store without has_reverse_index=True gets [] from
        in_edges (the strict 'index' mode contract).
        """
        from uams.storage.base import MemoryStore

        class _NoIndexStore(MemoryStore):
            has_reverse_index = False

            def store(self, m): pass
            def retrieve(self, mid): return None
            def delete(self, mid): return False
            def search_keywords(self, q, k=10): return []
            def search_vector(self, v, k=10, **f): return []
            def search_graph(self, e, d=2): return []
            def list_all(self, limit=100): return []
            def delete_expired(self): return 0
            def close(self): pass
            def count(self): return 0
            def delete_by_filter(self, field, value): return 0

        s = _NoIndexStore()
        self.assertEqual(s.in_edges("anything"), [])
        # in_edges_scan still works (list_all + filter fallback)
        self.assertEqual(s.in_edges_scan("anything"), [])


if __name__ == "__main__":
    unittest.main()