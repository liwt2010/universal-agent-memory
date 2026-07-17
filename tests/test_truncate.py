"""Regression test for T02 (P0-2): truncate() replaces
list_all(999999) + delete() pattern.

Pins:
- InMemoryStore.truncate() deletes every memory
- SQLiteStore.truncate() deletes every memory (the v0.5.x bug
  was list_all(999) silently dropped everything past 999)
- UniversalMemorySystem.clear() drops every memory in every
  tier (not just the first 999 on SQLite)
- MigrationTool.migrate() doesn't silently drop the source
  past row 999
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
    TemporalAnchor,
)
from uams.storage.memory import InMemoryStore
from uams.storage.sqlite import SQLiteStore
from uams.system import UniversalMemorySystem
from uams.utils.backup import MigrationTool


def _make_mem(idx: int, *, project_id: str = "p") -> Memory:
    return Memory(
        id=MemoryId(f"m-{idx:04d}"),
        anchor=TemporalAnchor(),
        context=AgentContext(
            agent_id="a", agent_type="t", session_id="s", user_id="u",
            project_id=project_id,
        ),
        payload=MemoryPayload(raw=f"row {idx}", structured={}, embedding=None),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
        ),
    )


class TestTruncate(unittest.TestCase):
    def test_inmemory_truncate_deletes_all(self) -> None:
        s = InMemoryStore()
        for i in range(50):
            s.store(_make_mem(i))
        self.assertEqual(s.count(), 50)
        deleted = s.truncate()
        self.assertEqual(deleted, 50)
        self.assertEqual(s.count(), 0)

    def test_inmemory_truncate_empty(self) -> None:
        s = InMemoryStore()
        self.assertEqual(s.truncate(), 0)

    def test_sqlite_truncate_deletes_all_1050_rows(self) -> None:
        """The core P0-2 regression: the v0.5.x clear() walked
        list_all(999) and silently dropped everything past row
        999. SQLiteStore.truncate() uses DELETE FROM and clears
        the entire table in a single round-trip.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = SQLiteStore(db_path=path, tier_name="test")
        n = 1050
        for i in range(n):
            s.store(_make_mem(i))
        self.assertEqual(s.count(), n)

        deleted = s.truncate()
        self.assertEqual(deleted, n)
        self.assertEqual(s.count(), 0)
        s.close()
        _cleanup_db(path)

    def test_system_clear_deletes_every_tier(self) -> None:
        """UniversalMemorySystem.clear() must clear every tier, not
        just the first 999 rows of each.
        """
        u = UniversalMemorySystem()
        try:
            for i in range(50):
                from uams import AgentEvent, EventType
                u.observe(AgentEvent(
                    event_type=EventType.USER_INPUT,
                    agent_context=AgentContext(
                        agent_id=f"a{i}", agent_type="t", session_id="s", user_id="u",
                    ),
                    content=f"event {i}",
                ))
            u.clear()
            for store in u._stores.values():
                self.assertEqual(store.count(), 0)
        finally:
            u.shutdown()

    def test_migrate_does_not_silently_drop_rows(self) -> None:
        """MigrationTool.migrate() must move every source row to
        target, not just the first 999.
        """
        fd, src_path = tempfile.mkstemp(suffix=".db")
        fd2, dst_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.close(fd2)
        try:
            src = SQLiteStore(db_path=src_path, tier_name="src")
            dst = InMemoryStore()
            n = 1100
            for i in range(n):
                src.store(_make_mem(i))

            migrated = MigrationTool().migrate(src, dst, batch_size=500)
            self.assertEqual(migrated, n)
            self.assertEqual(dst.count(), n)
            src.close()
        finally:
            _cleanup_db(src_path)
            _cleanup_db(dst_path)


def _cleanup_db(path: str) -> None:
    """Remove SQLite DB and its WAL / SHM companions. Retries once
    on Windows file-locking races."""
    import time
    for ext in ("", "-wal", "-shm"):
        full = path + ext
        if not os.path.exists(full):
            continue
        for _ in range(3):
            try:
                os.unlink(full)
                break
            except PermissionError:
                time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()