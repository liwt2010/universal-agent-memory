"""Tests for utils.backup — failure-path semantics.

BackupManager.backup_to_file and restore_from_file used to silently
return 0 on any exception, conflating the empty-store / zero-valid-lines
case with a fatal failure. The fix returns None on failure so the caller
can distinguish "no work" from "something broke".
"""

import os
import sys
import tempfile
import unittest

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.utils.backup import BackupManager
from uams.storage.memory import InMemoryStore
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata,
)
from uams.core.enums import MemoryType, PrivacyLevel


def _make_memory(raw: str) -> Memory:
    return Memory(
        id=MemoryId(),
        anchor=TemporalAnchor(),
        context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
        payload=MemoryPayload(raw=raw),
        metadata=MemoryMetadata(
            memory_type=MemoryType.WORKING,
            privacy=PrivacyLevel.PUBLIC,
        ),
    )


class TestBackupFailureSemantics(unittest.TestCase):
    """Backup and restore must signal failure with None, not 0."""

    def test_backup_to_file_returns_none_on_failure(self):
        """If the file cannot be written, return None (not 0)."""
        store = InMemoryStore(max_capacity=10)
        store.store(_make_memory("hello"))
        manager = BackupManager(store)
        # Path on Windows that we know can't be created: a directory used as file
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "a_directory_not_a_file")
            os.makedirs(bad_path)
            result = manager.backup_to_file(bad_path)
        self.assertIsNone(
            result,
            "backup_to_file must return None on failure (got %r)" % (result,),
        )

    def test_backup_to_file_returns_zero_for_empty_store(self):
        """Empty store with valid file path: success returns 0, not None."""
        store = InMemoryStore(max_capacity=10)
        manager = BackupManager(store)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            path = f.name
        try:
            result = manager.backup_to_file(path)
        finally:
            os.unlink(path)
        # 0 means "no memories to export", which is a valid success state
        self.assertEqual(result, 0)
        self.assertIsNotNone(result)

    def test_restore_from_file_returns_none_on_failure(self):
        """If the file cannot be opened, return None (not 0)."""
        store = InMemoryStore(max_capacity=10)
        manager = BackupManager(store)
        result = manager.restore_from_file("/nonexistent/path/does/not/exist.jsonl")
        self.assertIsNone(result)

    def test_restore_from_file_returns_zero_for_empty_file(self):
        """Empty file: success returns 0 (zero valid lines), not None."""
        store = InMemoryStore(max_capacity=10)
        manager = BackupManager(store)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            path = f.name
        try:
            result = manager.restore_from_file(path)
        finally:
            os.unlink(path)
        # 0 valid lines in an otherwise-readable file is a success
        self.assertEqual(result, 0)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
