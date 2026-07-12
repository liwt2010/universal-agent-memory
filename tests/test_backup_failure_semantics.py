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


class TestRestoreErrorClassification(unittest.TestCase):
    """P1-CON-4: backup.restore_from_file must distinguish JSON parse
    failures (truncated backup → skip line) from store write failures
    (disk full / connection lost → abort whole restore). The previous
    implementation logged both as 'Skipped invalid backup line' which
    misdirected operators toward the wrong layer."""

    def _write_jsonl(self, tmp_path, lines):
        path = os.path.join(tmp_path, "backup.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")
        return path

    def test_malformed_json_line_skipped_with_warning(self):
        """A truncated JSON line must be skipped, not abort the restore."""
        from uams.core.models import (
            AgentContext, Memory, MemoryId, MemoryMetadata,
            MemoryPayload, TemporalAnchor,
        )
        from uams.core.enums import MemoryType, PrivacyLevel

        good = Memory(
            id=MemoryId("good-1"),
            anchor=TemporalAnchor(),
            context=AgentContext("a", "t", "s"),
            payload=MemoryPayload(raw="hello"),
            metadata=MemoryMetadata(MemoryType.WORKING, PrivacyLevel.PUBLIC),
        ).to_json()
        import json as _json
        good_line = _json.dumps(good)

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_jsonl(
                tmp,
                ["{not valid json", good_line],
            )
            store = InMemoryStore(max_capacity=10)
            manager = BackupManager(store)
            result = manager.restore_from_file(path)

        # The good line was imported despite the malformed one before it.
        self.assertEqual(result, 1)
        self.assertIsNotNone(store.retrieve("good-1"))

    def test_store_failure_mid_restore_aborts_returns_none(self):
        """A store failure on line N must abort and return None, not silently
        report partial-success as '0 imported'."""
        from uams.core.models import (
            AgentContext, Memory, MemoryId, MemoryMetadata,
            MemoryPayload, TemporalAnchor,
        )
        from uams.core.enums import MemoryType, PrivacyLevel

        def make_line(mid):
            mem = Memory(
                id=MemoryId(mid),
                anchor=TemporalAnchor(),
                context=AgentContext("a", "t", "s"),
                payload=MemoryPayload(raw=mid),
                metadata=MemoryMetadata(MemoryType.WORKING, PrivacyLevel.PUBLIC),
            )
            import json as _json
            return _json.dumps(mem.to_json())

        # Build a store that fails on the 2nd store() call.
        store = InMemoryStore(max_capacity=10)
        original_store = store.store
        call_count = [0]

        def maybe_fail(mem):
            call_count[0] += 1
            if call_count[0] == 2:
                raise IOError("simulated disk full")
            return original_store(mem)

        store.store = maybe_fail

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_jsonl(
                tmp,
                [make_line("m1"), make_line("m2"), make_line("m3")],
            )
            manager = BackupManager(store)
            result = manager.restore_from_file(path)

        # Mid-restore failure → None (not partial count, not 0).
        self.assertIsNone(result)
        # m1 was imported (before the failure), m3 was not.
        self.assertIsNotNone(store.retrieve("m1"))
        self.assertIsNone(store.retrieve("m3"))


if __name__ == "__main__":
    unittest.main()
