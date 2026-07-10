"""Tests for cross-layer forget cascade deletion."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.config import UAMSConfig
from uams.core.enums import MemoryType, PrivacyLevel
from uams.core.models import (
    AgentContext, Memory, MemoryId, MemoryMetadata, MemoryPayload,
    Relation, TemporalAnchor,
)
from uams.pipeline.cascade import (
    CascadeReport,
    CascadeStrategy,
    CascadeForgetter,
)
from uams.storage.base import MemoryStore
try:
    from uams.utils.cascade_audit import CascadeAuditWriter
    _HAS_AUDIT = True
except ImportError:                               # Task 2 will create this
    CascadeAuditWriter = None                     # type: ignore
    _HAS_AUDIT = False


# --- Test doubles ---------------------------------------------------------

class _FakeMemory(Memory):
    """Memory constructor helper for tests."""


def _make_mem(memory_id: str, raw: str, relations=None, tier: MemoryType = MemoryType.SEMANTIC) -> Memory:
    return Memory(
        id=MemoryId(memory_id),
        anchor=TemporalAnchor(created_at=12345.0),
        context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
        payload=MemoryPayload(raw=raw),
        metadata=MemoryMetadata(
            memory_type=tier,
            privacy=PrivacyLevel.PUBLIC,
            importance=5.0, confidence=0.95,
            relations=[Relation(r["type"], r["target_memory_id"], strength=r.get("strength", 1.0))
                       for r in (relations or [])],
        ),
    )


class _InMemStore(MemoryStore):
    """In-memory store that satisfies MemoryStore. Per-instance tier label."""
    def __init__(self, tier: MemoryType):
        self._tier = tier
        self._mem: Dict[str, Memory] = {}

    @property
    def tier(self) -> MemoryType:
        return self._tier

    def store(self, memory: Memory) -> None:
        self._mem[str(memory.id)] = memory

    def retrieve(self, memory_id: str) -> Optional[Memory]:
        return self._mem.get(memory_id)

    def delete(self, memory_id: str) -> bool:
        if memory_id in self._mem:
            del self._mem[memory_id]
            return True
        return False

    def search_keywords(self, query: str, k: int = 10) -> List[Memory]:
        return [m for m in self._mem.values() if query.lower() in m.payload.raw.lower()][:k]

    def search_vector(self, vector, k: int = 10, **filters) -> List[Memory]:
        return list(self._mem.values())[:k]

    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        return []

    def list_all(self, limit: int = 100) -> List[Memory]:
        return list(self._mem.values())[:limit]

    def delete_expired(self) -> int:
        return 0

    def get(self, mid: str) -> Optional[Memory]:
        return self._mem.get(mid)


# --- Tests ----------------------------------------------------------------

class TestCascadeStrategyEnum(unittest.TestCase):
    def test_values_round_trip(self):
        self.assertEqual(CascadeStrategy("isolated"), CascadeStrategy.ISOLATED)
        self.assertEqual(CascadeStrategy("outgoing"), CascadeStrategy.OUTGOING)
        self.assertEqual(CascadeStrategy("bidirectional"), CascadeStrategy.BIDIRECTIONAL)

    def test_json_serializable(self):
        for s in (CascadeStrategy.ISOLATED, CascadeStrategy.OUTGOING, CascadeStrategy.BIDIRECTIONAL):
            self.assertEqual(json.dumps(s), f'"{s.value}"')

    def test_accepts_string_or_enum(self):
        self.assertEqual(CascadeStrategy("isolated"), CascadeStrategy.ISOLATED)
        self.assertEqual(CascadeStrategy(CascadeStrategy.BIDIRECTIONAL), CascadeStrategy.BIDIRECTIONAL)


class TestCascadeReportDataclass(unittest.TestCase):
    def _make_report(self, **kw) -> CascadeReport:
        defaults = dict(
            target_id="t", tier=MemoryType.SEMANTIC,
            strategy=CascadeStrategy.BIDIRECTIONAL,
            deleted_ids=["a", "b"],
            orphan_ids=[("x", "a")],
            failed_ids=[("y", "boom")],
            duration_ms=10.0,
            audit_log_path=Path("/tmp/a.jsonl"),
        )
        defaults.update(kw)
        return CascadeReport(**defaults)

    def test_count_properties(self):
        r = self._make_report()
        self.assertEqual(r.deleted_count, 2)
        self.assertEqual(r.orphan_count, 1)
        self.assertEqual(r.failed_count, 1)

    def test_is_complete_false_when_failures(self):
        r = self._make_report(failed_ids=[("a", "boom")])
        self.assertFalse(r.is_complete)
        r2 = self._make_report(failed_ids=[])
        self.assertTrue(r2.is_complete)

    def test_to_dict_shape(self):
        r = self._make_report()
        d = r.to_dict()
        self.assertIn("ts", d)
        self.assertEqual(d["action"], "cascade_forget")
        self.assertEqual(d["target_id"], "t")
        self.assertEqual(d["tier"], "SEMANTIC")
        self.assertEqual(d["strategy"], "bidirectional")
        self.assertEqual(d["deleted_count"], 2)
        self.assertEqual(d["is_complete"], False)
        self.assertEqual(d["orphan_ids"], [["x", "a"]])
        self.assertEqual(d["failed_ids"], [["y", "boom"]])


class TestCascadeForgetterIsolated(unittest.TestCase):
    def test_strategy_isolated_only_target(self):
        self.skipTest("Task 5 will implement.")


class TestAuditLogAppend(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="uams-cascade-test-")
        self.path = Path(self._tmpdir) / "audit.jsonl"
        self.writer = CascadeAuditWriter(self.path,
                                         orphan_path=self.path.parent / "orphan.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_lazy_dir_creation(self):
        nested = Path(self._tmpdir) / "deep" / "nested" / "audit.jsonl"
        w = CascadeAuditWriter(nested)
        w.append({"k": "v"})
        self.assertTrue(nested.exists())

    def test_one_jsonl_line_per_call(self):
        for i in range(3):
            self.writer.append({"i": i, "action": "cascade_forget"})
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3)
        for i, ln in enumerate(lines):
            self.assertEqual(json.loads(ln)["i"], i)

    def test_orphan_log_dual_writer(self):
        self.writer.append_orphan({"orphan_id": "x", "parent_id": "p"})
        orphan_path = self.path.parent / "orphan.jsonl"
        self.assertTrue(orphan_path.exists())
        line = orphan_path.read_text(encoding="utf-8").strip()
        self.assertEqual(json.loads(line)["orphan_id"], "x")


class TestAuditConcurrency(unittest.TestCase):
    def test_no_interleaved_lines_under_concurrent_writes(self):
        tmpdir = tempfile.mkdtemp(prefix="uams-cascade-conc-")
        path = Path(tmpdir) / "audit.jsonl"
        w = CascadeAuditWriter(path)
        n_threads = 8
        per_thread = 50

        def worker(tid):
            for j in range(per_thread):
                w.append({"t": tid, "j": j})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads: t.start()
        for t in threads: t.join()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), n_threads * per_thread)
        for ln in lines:
            json.loads(ln)


class TestConfigCascadeFields(unittest.TestCase):
    def test_defaults(self):
        from uams.config import UAMSConfig
        c = UAMSConfig()
        self.assertEqual(c.cascade_in_edge_strategy, "auto")
        self.assertEqual(c.cascade_max_depth, 4)
        self.assertEqual(c.cascade_audit_log_path, "logs/cascade_forget_audit.jsonl")
        self.assertEqual(c.cascade_orphan_log_path, "logs/cascade_orphan_log.jsonl")

    def test_can_override(self):
        from uams.config import UAMSConfig
        c = UAMSConfig(
            cascade_in_edge_strategy="scan",
            cascade_max_depth=8,
            cascade_audit_log_path="custom/audit.jsonl",
        )
        self.assertEqual(c.cascade_in_edge_strategy, "scan")
        self.assertEqual(c.cascade_max_depth, 8)
        self.assertEqual(c.cascade_audit_log_path, "custom/audit.jsonl")


if __name__ == "__main__":
    unittest.main()
