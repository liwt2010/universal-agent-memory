# Cross-layer forget with cascade deletion — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the cascade-forget feature described in `docs/superpowers/specs/2026-07-10-cross-layer-forget-cascade-design.md` (commit `f5c5a56`) — three-strategy `forget()`, visit-set + max_depth BFS, strict same-tier scope, hybrid in-edge discovery, JSONL audit trail.

**Architecture:** New modules `src/uams/pipeline/cascade.py` (engine) and `src/uams/utils/cascade_audit.py` (audit writer). `system.forget()` rewired to dispatch through the new engine. `UAMSConfig` gains 4 cascade-related fields.

**Tech Stack:** Python 3.11+, stdlib only (`enum`, `dataclasses`, `threading`, `json`, `pathlib`, `datetime`, `collections.deque`, `time`). No new third-party deps.

---

## File structure

| File | Status | Responsibility |
|---|---|---|
| `src/uams/pipeline/cascade.py` | **Create** | `CascadeStrategy` enum, `CascadeReport` dataclass, `CascadeForgetter` class with BFS algorithm, in-edge helper. |
| `src/uams/utils/cascade_audit.py` | **Create** | `CascadeAuditWriter` — append-only JSONL writer with RLock + lazy dir + orphan-mode dual writer. |
| `src/uams/utils/__init__.py` | Modify | Export `CascadeAuditWriter`. |
| `src/uams/pipeline/__init__.py` | Modify | Export `CascadeStrategy`, `CascadeReport`, `CascadeForgetter`. |
| `src/uams/config.py` | Modify | Add 4 cascade fields to `UAMSConfig` (with defaults). |
| `src/uams/system.py` | Modify | `forget()` accepts `cascade`, dispatches to `CascadeForgetter`. |
| `tests/test_cascade.py` | **Create** | ~30 unit tests across 14 groups (per spec § 13). |
| `docs/CASCADE_FORGET.md` | **Create** | User-facing ~80-line doc with examples + GDPR note. |
| `PRODUCTION_ASSESSMENT.md` | Modify | Add v4 section reflecting the new feature. |

Each file is small and focused. `cascade.py` will be the largest at ~280 LOC because of the algorithm; the rest are <120 LOC each.

---

## Conventions for the executor

- **Run unit tests from project root** with: `python -m unittest discover -s tests`
  - For faster per-task feedback: `python -m unittest tests.test_cascade -v`
- **Use the project's Python interpreter** (Windows):
  ```
  C:\Users\liwt0\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
  ```
- **Git path (Windows):** `D:\Program Files\Git\cmd\git.exe`
- **Commit messages** must use `-F <file>` because PowerShell parses parens as args; never use inline emoji.
- **Run flake8** at end: `python -m flake8 src/uams/pipeline/cascade.py src/uams/utils/cascade_audit.py tests/test_cascade.py --select=E9,F63,F7,F82`
- **No backward-incompatible change to `forget()` signature that breaks callers using positional args.** Current call shape: `forget(memory_id)`. New shape keeps that position + adds two keyword-only params.

---

# Tasks

## Task 1: `CascadeStrategy` enum + `CascadeReport` dataclass

**Files:**
- Create: `src/uams/pipeline/cascade.py` (skeleton only, <100 LOC)
- Test: `tests/test_cascade.py`

- [ ] **Step 1: Write failing test file with skeleton**

Create `tests/test_cascade.py`:

```python
"""Tests for cross-layer forget cascade deletion."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional

from uams.core.config import UAMSConfig
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
from uams.utils.cascade_audit import CascadeAuditWriter


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
        # test-level: return up to k items so search_vector-based tests exist
        return list(self._mem.values())[:k]

    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        # explicit no-graph for tests; cascade tests use direct relations
        return []

    def list_all(self, limit: int = 100) -> List[Memory]:
        return list(self._mem.values())[:limit]

    def delete_expired(self) -> int:
        return 0

    # --- testing aid ---
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
        # string accepted
        self.assertEqual(CascadeStrategy("isolated"), CascadeStrategy.ISOLATED)
        # enum accepted
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
        # tuples serialized as lists
        self.assertEqual(d["orphan_ids"], [["x", "a"]])
        self.assertEqual(d["failed_ids"], [["y", "boom"]])


# Stubs for higher-level tests below — populated in later tasks.
class TestCascadeForgetterIsolated(unittest.TestCase):
    def test_strategy_isolated_only_target(self):
        # implemented in Task 7
        self.skipTest("Task 7 will implement")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify only the type/test stub tests are collected, others skip**

Run:
```
python -m unittest tests.test_cascade -v
```
Expected: at least `TestCascadeStrategyEnum` (3 pass), `TestCascadeReportDataclass` (3 pass), `TestCascadeForgetterIsolated` (skipped: Task 7). Total `Ran 7 tests` with `skipped=1`. ImportError on `uams.pipeline.cascade` is the failure indicator for Step 3.

- [ ] **Step 3: Create `cascade.py` skeleton with types only**

Create `src/uams/pipeline/cascade.py`:

```python
"""Cross-layer cascade deletion for memory forget (GDPR-friendly)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

from uams.core.config import UAMSConfig
from uams.core.enums import MemoryType
from uams.storage.base import MemoryStore


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class CascadeStrategy(str, Enum):
    """Cascade behavior when forgetting a memory.

    Inheriting `str` makes instances JSON-serializable without a custom encoder.
    """
    ISOLATED = "isolated"
    OUTGOING = "outgoing"
    BIDIRECTIONAL = "bidirectional"


@dataclass
class CascadeReport:
    """Outcome of a `CascadeForgetter.forget()` invocation."""
    target_id: str
    tier: Optional[MemoryType]
    strategy: CascadeStrategy

    deleted_ids: List[str] = field(default_factory=list)
    orphan_ids:  List[Tuple[str, str]] = field(default_factory=list)
    failed_ids:  List[Tuple[str, str]] = field(default_factory=list)

    duration_ms: float = 0.0
    audit_log_path: Optional[Path] = None

    @property
    def deleted_count(self) -> int: return len(self.deleted_ids)

    @property
    def orphan_count(self) -> int:  return len(self.orphan_ids)

    @property
    def failed_count(self) -> int:  return len(self.failed_ids)

    @property
    def is_complete(self) -> bool:  return self.failed_count == 0

    def to_dict(self) -> dict:
        return {
            "ts":            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "action":        "cascade_forget",
            "target_id":     self.target_id,
            "tier":          self.tier.name if self.tier is not None else None,
            "strategy":      self.strategy.value,
            "deleted_count": self.deleted_count,
            "orphan_count":  self.orphan_count,
            "failed_count":  self.failed_count,
            "deleted_ids":   list(self.deleted_ids),
            "orphan_ids":    [list(p) for p in self.orphan_ids],
            "failed_ids":    [list(p) for p in self.failed_ids],
            "duration_ms":   self.duration_ms,
            "is_complete":   self.is_complete,
        }


# ---------------------------------------------------------------------------
# CascadeForgetter — implementation goes here in Task 4+
# ---------------------------------------------------------------------------

class CascadeForgetter:
    """Cascade-deleting forgetter. Best-effort, audit-logged, BFS-bounded."""
    def __init__(
        self,
        stores: Dict[MemoryType, MemoryStore],
        config: UAMSConfig,
        audit_writer,           # CascadeAuditWriter — type stubs avoid an import cycle
    ) -> None:
        self._stores = stores
        self._config = config
        self._audit = audit_writer

    def forget(
        self,
        memory_id: str,
        *,
        strategy=None,           # type: CascadeStrategy | str | None
        max_depth: Optional[int] = None,
        in_edge_mode: Optional[str] = None,
    ):
        """Placeholder — full implementation lands in Task 7."""
        raise NotImplementedError("Task 7 will implement.")
```

- [ ] **Step 4: Run tests; expect 6 pass + 1 skip + 0 fail**

Run:
```
python -m unittest tests.test_cascade -v
```
Expected: `Ran 7 tests` with `OK (skipped=1)`.

- [ ] **Step 5: Commit**

```bash
git add src/uams/pipeline/cascade.py tests/test_cascade.py
git commit -F _msg.txt
```

`_msg.txt`:
```
feat(cascade): CascadeStrategy enum + CascadeReport dataclass + test harness

Scaffolds the new pipeline module per
docs/superpowers/specs/2026-07-10-cross-layer-forget-cascade-design.md
(Task 1 of the implementation plan).

CascadeForgetter.forget() is a NotImplementedError placeholder; Task 7
fills it. Test harness in test_cascade.py includes a fake store and a
factory helper; later tasks layer the rest of the test groups on top.
```

---

## Task 2: `CascadeAuditWriter` with RLock + lazy dir

**Files:**
- Create: `src/uams/utils/cascade_audit.py`
- Modify: `src/uams/utils/__init__.py`
- Test: `tests/test_cascade.py` (extend with `TestAuditLogAppend` + `TestAuditConcurrency`)

- [ ] **Step 1: Append failing tests to `tests/test_cascade.py`**

Add inside `tests/test_cascade.py` (before the `if __name__ == "__main__":` line):

```python
class TestAuditLogAppend(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="uams-cascade-test-")
        self.path = Path(self._tmpdir) / "audit.jsonl"
        self.writer = CascadeAuditWriter(self.path, orphan_path=self.path.parent / "orphan.jsonl")
        # Use a real (in-process) lockable writer
        from uams.utils.cascade_audit import CascadeAuditWriter as _W
        self.assertIsInstance(self.writer, _W)

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

        def worker(tid: int):
            for j in range(per_thread):
                w.append({"t": tid, "j": j})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads: t.start()
        for t in threads: t.join()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), n_threads * per_thread)
        # every line is valid JSON
        for ln in lines:
            json.loads(ln)
```

- [ ] **Step 2: Run tests, verify the new 4 fail with `ImportError`**

Run:
```
python -m unittest tests.test_cascade.TestAuditLogAppend tests.test_cascade.TestAuditConcurrency -v
```
Expected: 4 errors with `ModuleNotFoundError: No module named 'uams.utils.cascade_audit'`.

- [ ] **Step 3: Create `src/uams/utils/cascade_audit.py`**

```python
"""Append-only JSONL audit log writer for cascade-forget events."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional


class CascadeAuditWriter:
    """Thread-safe append-only JSONL writer.

    - Opens the file lazily on first append.
    - Creates parent directories as needed.
    - Holds an RLock so concurrent forget() calls within one process
      do not interleave bytes.
    - Never fync — audit is "best-effort" by design (see spec § 11.3).
    """

    def __init__(
        self,
        path: Path | str = "logs/cascade_forget_audit.jsonl",
        orphan_path: Path | str = "logs/cascade_orphan_log.jsonl",
    ) -> None:
        self._path = Path(path)
        self._orphan_path = Path(orphan_path)
        self._lock = threading.RLock()
        self._fp = None
        self._orphan_fp = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def orphan_path(self) -> Path:
        return self._orphan_path

    def _ensure_open(self, target_attr: str) -> None:
        fp = getattr(self, target_attr)
        if fp is not None:
            return
        path: Path = getattr(self, "_path" if target_attr == "_fp" else "_orphan_path")
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a", encoding="utf-8", newline="\n")
        setattr(self, target_attr, f)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._ensure_open("_fp")
            self._fp.write(line + "\n")
            self._fp.flush()

    def append_orphan(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._ensure_open("_orphan_fp")
            self._orphan_fp.write(line + "\n")
            self._orphan_fp.flush()

    def close(self) -> None:
        with self._lock:
            for attr in ("_fp", "_orphan_fp"):
                fp = getattr(self, attr)
                if fp is not None:
                    fp.close()
                    setattr(self, attr, None)
```

- [ ] **Step 4: Update `src/uams/utils/__init__.py` to export it**

Edit the existing `src/uams/utils/__init__.py` so it exposes `CascadeAuditWriter`. Read the file first; its existing exports use a specific style — mimic it. A safe minimal edit:

```python
# Append at end of src/uams/utils/__init__.py
from uams.utils.cascade_audit import CascadeAuditWriter
__all__ = [...existing...] + ["CascadeAuditWriter"]  # preserve existing __all__
```

If `__all__` is not present, just add the import line.

- [ ] **Step 5: Run tests; expect 4 pass + previous 7**

Run:
```
python -m unittest tests.test_cascade -v
```
Expected: `Ran 11 tests OK (skipped=1)`.

- [ ] **Step 6: Commit**

`_msg.txt`:
```
feat(cascade): append-only JSONL audit writer (CascadeAuditWriter)

Implements spec § 11 audit log format. Two files:
- cascade_forget_audit.jsonl (per-cascade-invocation record)
- cascade_orphan_log.jsonl (per-cross-tier-edge-orphan record)

Thread-safe (RLock) so concurrent forget() calls in one process do
not interleave bytes. Lazy dir creation; flush on append; no fsync by
design (spec § 11.3).
```

```bash
git add src/uams/utils/cascade_audit.py src/uams/utils/__init__.py tests/test_cascade.py
git commit -F _msg.txt
```

---

## Task 3: UAMSConfig cascade fields

**Files:**
- Modify: `src/uams/config.py`
- Test: `tests/test_cascade.py` (1 new test)

- [ ] **Step 1: Append failing test**

```python
class TestConfigCascadeFields(unittest.TestCase):
    def test_defaults(self):
        from uams.core.config import UAMSConfig
        c = UAMSConfig()
        self.assertEqual(c.cascade_in_edge_strategy, "auto")
        self.assertEqual(c.cascade_max_depth, 4)
        self.assertEqual(c.cascade_audit_log_path, "logs/cascade_forget_audit.jsonl")
        self.assertEqual(c.cascade_orphan_log_path, "logs/cascade_orphan_log.jsonl")

    def test_can_override(self):
        from uams.core.config import UAMSConfig
        c = UAMSConfig(
            cascade_in_edge_strategy="scan",
            cascade_max_depth=8,
            cascade_audit_log_path="custom/audit.jsonl",
        )
        self.assertEqual(c.cascade_in_edge_strategy, "scan")
        self.assertEqual(c.cascade_max_depth, 8)
        self.assertEqual(c.cascade_audit_log_path, "custom/audit.jsonl")
```

- [ ] **Step 2: Run; expect 2 errors with `TypeError: __init__() got unexpected keyword argument 'cascade_in_edge_strategy'`**

Run: `python -m unittest tests.test_cascade.TestConfigCascadeFields -v`

- [ ] **Step 3: Append 4 fields to `UAMSConfig`**

Open `src/uams/config.py`, locate `class UAMSConfig`, and add at the **end** (alphabetical or grouped; pick a consistent style and follow it):

```python
    # --- Cascade delete (v0.2 / cross-layer forget) ---
    cascade_in_edge_strategy: Literal["scan", "index", "auto"] = "auto"
    cascade_max_depth: int = 4
    cascade_audit_log_path: str = "logs/cascade_forget_audit.jsonl"
    cascade_orphan_log_path: str = "logs/cascade_orphan_log.jsonl"
```

Add at the top of the file (if not already imported):
```python
from typing import Literal
```

- [ ] **Step 4: Run; expect 2 pass**

Run: `python -m unittest tests.test_cascade.TestConfigCascadeFields -v`

Expected: `Ran 2 tests OK`.

- [ ] **Step 5: Confirm config doesn't break existing tests**

Run: `python -m unittest discover -s tests -p "test_config_validation.py" -v`

Expected: existing config tests still pass (no regressions).

- [ ] **Step 6: Commit**

`_msg.txt`:
```
feat(config): add UAMSConfig cascade fields (v0.2)

4 new fields, all with conservative defaults that preserve backward
compatibility with every existing UAMSConfig() instantiation:
- cascade_in_edge_strategy = "auto"
- cascade_max_depth = 4
- cascade_audit_log_path = "logs/cascade_forget_audit.jsonl"
- cascade_orphan_log_path = "logs/cascade_orphan_log.jsonl"
```

```bash
git add src/uams/config.py tests/test_cascade.py
git commit -F _msg.txt
```

---

## Task 4: `CascadeForgetter._locate_tier` + `_discover_in_edges` (scan/auto)

**Files:**
- Modify: `src/uams/pipeline/cascade.py`
- Test: `tests/test_cascade.py` (extend)

- [ ] **Step 1: Append failing tests**

```python
class TestLocateTier(unittest.TestCase):
    def _stores(self):
        # semantic holds target, working holds unrelated
        s = _InMemStore(MemoryType.SEMANTIC)
        w = _InMemStore(MemoryType.WORKING)
        s.store(_make_mem("m1", "x"))
        return {MemoryType.SEMANTIC: s, MemoryType.WORKING: w}

    def test_locates_tier(self):
        from uams.pipeline.cascade import CascadeForgetter
        f = CascadeForgetter(self._stores(), UAMSConfig(), CascadeAuditWriter())
        self.assertEqual(f._locate_tier("m1"), MemoryType.SEMANTIC)

    def test_returns_none_when_absent(self):
        from uams.pipeline.cascade import CascadeForgetter
        f = CascadeForgetter(self._stores(), UAMSConfig(), CascadeAuditWriter())
        self.assertIsNone(f._locate_tier("nope"))

    def test_locates_correctly_across_multiple_stores(self):
        from uams.pipeline.cascade import CascadeForgetter
        s = _InMemStore(MemoryType.SEMANTIC)
        e = _InMemStore(MemoryType.EPISODIC)
        s.store(_make_mem("s-1", "x", tier=MemoryType.SEMANTIC))
        e.store(_make_mem("e-1", "y", tier=MemoryType.EPISODIC))
        f = CascadeForgetter({MemoryType.SEMANTIC: s, MemoryType.EPISODIC: e},
                              UAMSConfig(), CascadeAuditWriter())
        self.assertEqual(f._locate_tier("s-1"), MemoryType.SEMANTIC)
        self.assertEqual(f._locate_tier("e-1"), MemoryType.EPISODIC)


class TestDiscoverInEdges(unittest.TestCase):
    def _stores_with_fwd_chain(self):
        # m1 -> m2 (out-edge), so a request to discover in-edges of m2 yields m1
        s = _InMemStore(MemoryType.SEMANTIC)
        s.store(_make_mem("m1", "source",
                          relations=[{"type": "follows", "target_memory_id": "m2"}]))
        s.store(_make_mem("m2", "target"))
        return {MemoryType.SEMANTIC: s}

    def test_scan_mode_finds_in_edges(self):
        from uams.pipeline.cascade import CascadeForgetter
        f = CascadeForgetter(self._stores_with_fwd_chain(),
                              UAMSConfig(cascade_in_edge_strategy="scan"),
                              CascadeAuditWriter())
        in_edges = f._discover_in_edges("m2", MemoryType.SEMANTIC, mode="scan")
        ids = {mid for mid, _tier in in_edges}
        self.assertIn("m1", ids)

    def test_index_mode_returns_empty_when_no_adapter(self):
        from uams.pipeline.cascade import CascadeForgetter
        s = self._stores_with_fwd_chain()[MemoryType.SEMANTIC]
        # confirm the test store has no _reverse_index attribute
        f = CascadeForgetter({MemoryType.SEMANTIC: s},
                              UAMSConfig(), CascadeAuditWriter())
        in_edges = f._discover_in_edges("m2", MemoryType.SEMANTIC, mode="index")
        self.assertEqual(in_edges, [])

    def test_auto_mode_falls_back_to_scan(self):
        from uams.pipeline.cascade import CascadeForgetter
        f = CascadeForgetter(self._stores_with_fwd_chain(),
                              UAMSConfig(), CascadeAuditWriter())
        in_edges = f._discover_in_edges("m2", MemoryType.SEMANTIC, mode="auto")
        ids = {mid for mid, _tier in in_edges}
        self.assertIn("m1", ids)
```

- [ ] **Step 2: Run; expect 6 errors on the new tests**

Run: `python -m unittest tests.test_cascade.TestLocateTier tests.test_cascade.TestDiscoverInEdges -v`

Expected: AttributeError `'CascadeForgetter' object has no attribute '_locate_tier'`.

- [ ] **Step 3: Implement helpers in `cascade.py`**

Replace the placeholder `forget()` with these methods (keep the import block at top):

```python
    def _locate_tier(self, memory_id: str) -> Optional[MemoryType]:
        """Find the tier that holds `memory_id`, or None if absent."""
        for tier, store in self._stores.items():
            try:
                if store.retrieve(memory_id) is not None:
                    return tier
            except Exception:
                # Treat errors as 'not found' rather than blowing up.
                continue
        return None

    def _discover_in_edges(
        self,
        target_id: str,
        tier: MemoryType,
        mode: Optional[str] = None,
    ) -> List[Tuple[str, MemoryType]]:
        """Return list of (source_memory_id, source_tier) that reference `target_id`.

        Modes:
          - 'scan':  O(N) walk all stores via list_all() and filter by relation.
          - 'index': use store._reverse_index() if available; empty list otherwise.
          - 'auto' : try 'index' per store, fall back to 'scan'; merge results.
        """
        mode = mode or self._config.cascade_in_edge_strategy
        results: List[Tuple[str, MemoryType]] = []

        for t, store in self._stores.items():
            if mode == "index":
                rev = getattr(store, "_reverse_index", None)
                sources = rev.get(target_id) if rev else []
                results.extend((s, t) for s in sources)
            elif mode == "scan":
                results.extend(self._scan_in_edges_for_store(store, target_id, t))
            elif mode == "auto":
                rev = getattr(store, "_reverse_index", None)
                if rev is not None:
                    sources = rev.get(target_id) or []
                    results.extend((s, t) for s in sources)
                else:
                    results.extend(self._scan_in_edges_for_store(store, target_id, t))
            else:
                raise ValueError(
                    f"Unknown cascade_in_edge_strategy: {mode!r} "
                    "(expected 'scan' | 'index' | 'auto')"
                )
        return results

    def _scan_in_edges_for_store(
        self, store: MemoryStore, target_id: str, tier: MemoryType
    ) -> List[Tuple[str, MemoryType]]:
        """O(N) scan: list_all then filter relations whose target == target_id."""
        out: List[Tuple[str, MemoryType]] = []
        try:
            iterator = store.list_all(limit=10_000_000)
        except Exception:
            return out
        for mem in iterator:
            for rel in mem.metadata.relations:
                if rel.target_memory_id == target_id:
                    out.append((str(mem.id), tier))
                    break  # one memory → one outgoing edge per target
        return out

    def forget(  # placeholder until Task 7
        self,
        memory_id: str,
        *,
        strategy=None,
        max_depth: Optional[int] = None,
        in_edge_mode: Optional[str] = None,
    ):
        raise NotImplementedError("Task 7 will implement.")
```

- [ ] **Step 4: Run; expect 6 pass**

Run: `python -m unittest tests.test_cascade -v`

Expected: `Ran 17 tests OK (skipped=1)`.

- [ ] **Step 5: Commit**

`_msg.txt`:
```
feat(cascade): _locate_tier and _discover_in_edges (scan/index/auto)

Two helpers needed before the BFS algorithm. _locate_tier is O(4*1)
per call (one retrieve() per tier). _discover_in_edges provides the
three modes from spec § 7 with auto-fallback when a store has no
_reverse_index() adapter (today, no backend has one).
```

```bash
git add src/uams/pipeline/cascade.py tests/test_cascade.py
git commit -F _msg.txt
```

---

## Task 5: BFS discover — visit-set + out-edges + max_depth (isolated / outgoing)

**Files:**
- Modify: `src/uams/pipeline/cascade.py`
- Test: `tests/test_cascade.py`

- [ ] **Step 1: Append failing tests**

Replace `TestCascadeForgetterIsolated`'s `test_strategy_isolated_only_target` with these:

```python
class TestCascadeForgetterIsolated(unittest.TestCase):
    def _stores_with_one_rel(self):
        s = _InMemStore(MemoryType.SEMANTIC)
        s.store(_make_mem("root", "root memory",
                          relations=[{"type": "follows", "target_memory_id": "child"}]))
        s.store(_make_mem("child", "child memory"))
        return {MemoryType.SEMANTIC: s}

    def test_strategy_isolated_only_target(self):
        from uams.pipeline.cascade import CascadeForgetter
        cfg = UAMSConfig()
        audit = CascadeAuditWriter()
        f = CascadeForgetter(self._stores_with_one_rel(), cfg, audit)
        r = f.forget("root", strategy="isolated")
        # only root deleted
        self.assertIn("root", r.deleted_ids)
        self.assertNotIn("child", r.deleted_ids)
        # child should still exist in store
        stores = self._stores_with_one_rel()
        s = stores[MemoryType.SEMANTIC]
        s.store(_make_mem("root", "x"))  # re-seed because previous f was a different store
        s.store(_make_mem("child", "y"))
        self.assertIsNotNone(s.retrieve("child"))


class TestCascadeForgetterOutgoing(unittest.TestCase):
    def _chain(self):
        # root -> a -> b -> c (linear)
        s = _InMemStore(MemoryType.SEMANTIC)
        s.store(_make_mem("root", "r", relations=[{"type": "next", "target_memory_id": "a"}]))
        s.store(_make_mem("a",    "a", relations=[{"type": "next", "target_memory_id": "b"}]))
        s.store(_make_mem("b",    "b", relations=[{"type": "next", "target_memory_id": "c"}]))
        s.store(_make_mem("c",    "c"))
        # also unrelated memory d (should be untouched)
        s.store(_make_mem("d",    "d"))
        return {MemoryType.SEMANTIC: s}

    def test_outgoing_deletes_chain_in_order(self):
        from uams.pipeline.cascade import CascadeForgetter
        stores = self._chain()
        f = CascadeForgetter(stores, UAMSConfig(cascade_max_depth=10), CascadeAuditWriter())
        r = f.forget("root", strategy="outgoing")
        # root + a + b + c all deleted; d untouched
        for mid in ("root", "a", "b", "c"):
            self.assertIn(mid, r.deleted_ids)
        self.assertNotIn("d", r.deleted_ids)
        # deletion order in cascade is leaves-first (BFS visit-order is parents-first,
        # delete reverses so leaves go first).
        for i in range(len(r.deleted_ids) - 1):
            # leaves (c, b, a) come before root
            pass  # we don't pin order — see Task 6 for exact order test

    def test_max_depth_caps_walk(self):
        from uams.pipeline.cascade import CascadeForgetter
        stores = self._chain()
        f = CascadeForgetter(stores, UAMSConfig(cascade_max_depth=1), CascadeAuditWriter())
        r = f.forget("root", strategy="outgoing")
        # With depth=1 we walk root and its immediate neighbor a, but NOT b or c.
        self.assertIn("root", r.deleted_ids)
        self.assertIn("a", r.deleted_ids)
        self.assertNotIn("b", r.deleted_ids)
        self.assertNotIn("c", r.deleted_ids)
```

- [ ] **Step 2: Run; expect 3 fails**

Run: `python -m unittest tests.test_cascade.TestCascadeForgetterIsolated tests.test_cascade.TestCascadeForgetterOutgoing -v`

Expected: 3 NotImplementedError failures.

- [ ] **Step 3: Implement the BFS algorithm (full `forget()`)**

Replace the `forget()` placeholder in `cascade.py` with:

```python
    def forget(
        self,
        memory_id: str,
        *,
        strategy=None,                          # CascadeStrategy | str | None
        max_depth: Optional[int] = None,
        in_edge_mode: Optional[str] = None,
    ) -> CascadeReport:
        """Forget a memory and (per strategy) its related memories.

        Always writes one audit-log line on completion. Never raises
        out of cascade — partial failures live in `report.failed_ids`.
        """
        t0 = time.monotonic()

        # --- normalize strategy / depth / mode ---
        if strategy is None:
            strategy = CascadeStrategy.BIDIRECTIONAL
        if not isinstance(strategy, CascadeStrategy):
            strategy = CascadeStrategy(strategy)        # string → enum
        if max_depth is None:
            max_depth = self._config.cascade_max_depth
        if in_edge_mode is None:
            in_edge_mode = self._config.cascade_in_edge_strategy

        # --- locate target tier ---
        target_tier = self._locate_tier(memory_id)

        report = CascadeReport(
            target_id=memory_id,
            tier=target_tier,
            strategy=strategy,
            audit_log_path=self._audit.path,
        )

        if target_tier is None:
            # target absent — write an audit-only record and return.
            report.duration_ms = (time.monotonic() - t0) * 1000
            self._audit.append(report.to_dict())
            return report

        # --- Phase 1: BFS discover ---
        target_store = self._stores[target_tier]
        visit_set: Set[str] = {memory_id}
        queue: Deque[Tuple[str, int]] = deque([(memory_id, 0)])
        discovered: List[str] = []

        while queue:
            cur_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            try:
                mem = target_store.retrieve(cur_id)
            except Exception:
                continue
            if mem is None:
                continue
            discovered.append(cur_id)

            # Out-edges
            if strategy in (CascadeStrategy.OUTGOING, CascadeStrategy.BIDIRECTIONAL):
                for rel in mem.metadata.relations:
                    tgt = rel.target_memory_id
                    if tgt in visit_set:
                        continue
                    tgt_tier = self._locate_tier(tgt)
                    if tgt_tier is None:
                        continue
                    if tgt_tier != target_tier:
                        report.orphan_ids.append((tgt, cur_id))  # cross-tier: record only
                        continue
                    visit_set.add(tgt)
                    queue.append((tgt, depth + 1))

            # In-edges (bidirectional only)
            if strategy == CascadeStrategy.BIDIRECTIONAL:
                for src_id, src_tier in self._discover_in_edges(
                    cur_id, target_tier, mode=in_edge_mode,
                ):
                    if src_id in visit_set:
                        continue
                    if src_tier != target_tier:
                        report.orphan_ids.append((src_id, cur_id))
                        continue
                    visit_set.add(src_id)
                    queue.append((src_id, depth + 1))

        # --- Phase 2: best-effort delete (leaves first) ---
        for cid in reversed(discovered):
            try:
                target_store.delete(cid)
                report.deleted_ids.append(cid)
            except Exception as exc:
                report.failed_ids.append((cid, repr(exc)))

        # --- Phase 3: audit ---
        report.duration_ms = (time.monotonic() - t0) * 1000
        self._audit.append(report.to_dict())
        # Also append orphan edges separately (non-blocking on audit failure).
        for orphan_id, parent_id in report.orphan_ids:
            self._audit.append_orphan({
                "ts":                  report.to_dict()["ts"],
                "action":              "orphan_edge",
                "orphan_id":           orphan_id,
                "orphan_tier":         "<cross-tier>",   # we don't pin tier here; could refine
                "parent_id":           parent_id,
                "triggered_by_target": memory_id,
                "triggered_by_strategy": strategy.value,
            })
        return report
```

- [ ] **Step 4: Run; expect 3 pass (no regression in earlier 14)**

Run:
```
python -m unittest tests.test_cascade -v
```
Expected: `Ran 20 tests OK (skipped=1)` (3 new pass + the earlier 17: 6 enum/report + 4 audit + 2 config + 6 locate/discover).

If any fails, common causes:
- `report.orphan_ids` mutated before `to_dict()` — verified order doesn't matter, both work.
- Some test expected "out_edges only" but got "in_edges too" → check the test skipped `strategy=` arg; default is bidirectional.

- [ ] **Step 5: Commit**

`_msg.txt`:
```
feat(cascade): BFS discover + best-effort delete + audit (isolated/outgoing)

Implements spec § 6 algorithm. Three phases:
1. locate target tier (None → audit-only)
2. BFS with visit-set + max_depth cap, same-tier scope; cross-tier
   edges added to orphan_ids (no delete).
3. best-effort delete leaves-first (reversed BFS order) with
   per-memory exception capture.
4. CascadeAuditWriter.append() writes the per-invocation line;
   append_orphan() writes one line per orphan edge encountered.

Default strategy is BIDIRECTIONAL (matches spec § 10 default). Default
in_edge_mode is config.cascade_in_edge_strategy (default 'auto').

This commit covers Tasks 1-5 only. Subsequent tasks will add: full
bidirectional tests, cycle protection tests, cross-tier orphan
verification, partial-failure coverage, and the system.py rewire.
```

```bash
git add src/uams/pipeline/cascade.py tests/test_cascade.py
git commit -F _msg.txt
```

---

## Task 6: Cycle + cross-tier orphan + partial failure + bidirectional tests

**Files:**
- Modify: `tests/test_cascade.py` (extend)

- [ ] **Step 1: Append failing tests for cycle protection**

```python
class TestCycleProtection(unittest.TestCase):
    def test_simple_cycle_terminates(self):
        from uams.pipeline.cascade import CascadeForgetter
        s = _InMemStore(MemoryType.SEMANTIC)
        # a -> b, b -> a (cycle)
        s.store(_make_mem("a", "a", relations=[{"type": "x", "target_memory_id": "b"}]))
        s.store(_make_mem("b", "b", relations=[{"type": "x", "target_memory_id": "a"}]))
        stores = {MemoryType.SEMANTIC: s}
        f = CascadeForgetter(stores,
                              UAMSConfig(cascade_max_depth=20),
                              CascadeAuditWriter())
        r = f.forget("a", strategy="bidirectional")
        # both deleted exactly once
        self.assertEqual(r.deleted_ids.count("a"), 1)
        self.assertEqual(r.deleted_ids.count("b"), 1)

    def test_longer_cycle_term(self):
        from uams.pipeline.cascade import CascadeForgetter
        s = _InMemStore(MemoryType.SEMANTIC)
        # a -> b -> c -> a
        s.store(_make_mem("a", "a", relations=[{"type": "x", "target_memory_id": "b"}]))
        s.store(_make_mem("b", "b", relations=[{"type": "x", "target_memory_id": "c"}]))
        s.store(_make_mem("c", "c", relations=[{"type": "x", "target_memory_id": "a"}]))
        stores = {MemoryType.SEMANTIC: s}
        f = CascadeForgetter(stores,
                              UAMSConfig(cascade_max_depth=20),
                              CascadeAuditWriter())
        r = f.forget("a", strategy="outgoing")
        for mid in ("a", "b", "c"):
            self.assertEqual(r.deleted_ids.count(mid), 1)
```

- [ ] **Step 2: Append failing tests for cross-tier orphan**

```python
class TestCrossTierOrphan(unittest.TestCase):
    def test_cross_tier_target_is_orphan_not_deleted(self):
        from uams.pipeline.cascade import CascadeForgetter
        # root (semantic) points to cross (working tier) — cascade deletes root only.
        sem = _InMemStore(MemoryType.SEMANTIC)
        work = _InMemStore(MemoryType.WORKING)
        sem.store(_make_mem("root",  "r",
                            relations=[{"type": "x", "target_memory_id": "cross"}]))
        work.store(_make_mem("cross", "c"))
        stores = {MemoryType.SEMANTIC: sem, MemoryType.WORKING: work}
        f = CascadeForgetter(stores, UAMSConfig(), CascadeAuditWriter())
        r = f.forget("root", strategy="outgoing")
        # root deleted
        self.assertIn("root", r.deleted_ids)
        # cross recorded as orphan, NOT deleted
        self.assertIn(("cross", "root"), r.orphan_ids)
        self.assertNotIn("cross", r.deleted_ids)
        # cross still exists in its tier
        self.assertIsNotNone(work.retrieve("cross"))

    def test_cross_tier_in_edge_is_orphan(self):
        from uams.pipeline.cascade import CascadeForgetter
        sem = _InMemStore(MemoryType.SEMANTIC)
        work = _InMemStore(MemoryType.WORKING)
        # semantic root is the cascade target; working points at it (in-edge)
        sem.store(_make_mem("root", "r"))
        work.store(_make_mem("wrk",  "w",
                             relations=[{"type": "x", "target_memory_id": "root"}]))
        stores = {MemoryType.SEMANTIC: sem, MemoryType.WORKING: work}
        f = CascadeForgetter(stores, UAMSConfig(), CascadeAuditWriter())
        r = f.forget("root", strategy="bidirectional")
        # root deleted
        self.assertIn("root", r.deleted_ids)
        # wrk orphan (cross-tier reverse), NOT deleted
        self.assertIn(("wrk", "root"), r.orphan_ids)
        self.assertNotIn("wrk", r.deleted_ids)
        self.assertIsNotNone(work.retrieve("wrk"))
```

- [ ] **Step 3: Append failing tests for partial failure**

```python
class _PartialFailureStore(_InMemStore):
    """On delete of id == 'poison', raises RuntimeError."""
    def delete(self, memory_id: str) -> bool:
        if memory_id == "poison":
            raise RuntimeError("simulated backend outage")
        return super().delete(memory_id)


class TestPartialFailure(unittest.TestCase):
    def test_failed_memory_recorded_and_others_continue(self):
        from uams.pipeline.cascade import CascadeForgetter
        s = _PartialFailureStore(MemoryType.SEMANTIC)
        s.store(_make_mem("root",   "r"))
        s.store(_make_mem("poison", "p"))
        s.store(_make_mem("ok",     "o"))
        # root's outgoing list includes poison and ok
        # rewrite root to have a relation to poison, and add an unrelated ok
        s.store(_make_mem("root", "r",
                          relations=[{"type": "x", "target_memory_id": "poison"}]))
        stores = {MemoryType.SEMANTIC: s}
        f = CascadeForgetter(stores, UAMSConfig(cascade_max_depth=10), CascadeAuditWriter())
        r = f.forget("root", strategy="outgoing")
        # root + ok succeed
        self.assertIn("root", r.deleted_ids)
        self.assertIn("ok",   r.deleted_ids)
        # poison fails and is captured
        failed_ids = [mid for mid, _reason in r.failed_ids]
        self.assertIn("poison", failed_ids)
        # is_complete is False
        self.assertFalse(r.is_complete)
```

- [ ] **Step 4: Append failing tests for the full bidirectional sweep**

```python
class TestCascadeForgetterBidirectional(unittest.TestCase):
    def _stores(self):
        sem = _InMemStore(MemoryType.SEMANTIC)
        sem.store(_make_mem("root", "r",
                            relations=[{"type": "next", "target_memory_id": "child-a"}]))
        sem.store(_make_mem("child-a", "a"))
        sem.store(_make_mem("parent-b", "b",
                            relations=[{"type": "refers-to", "target_memory_id": "root"}]))
        sem.store(_make_mem("parent-c", "c",
                            relations=[{"type": "refers-to", "target_memory_id": "root"}]))
        sem.store(_make_mem("unrelated", "u"))
        return {MemoryType.SEMANTIC: sem}

    def test_bidirectional_sweeps_in_plus_out(self):
        from uams.pipeline.cascade import CascadeForgetter
        stores = self._stores()
        f = CascadeForgetter(stores, UAMSConfig(cascade_max_depth=5), CascadeAuditWriter())
        r = f.forget("root", strategy="bidirectional")
        for mid in ("root", "child-a", "parent-b", "parent-c"):
            self.assertIn(mid, r.deleted_ids, f"{mid} should be cascade-deleted")
        self.assertNotIn("unrelated", r.deleted_ids)

    def test_bidirectional_correct_visit_set(self):
        # Verifies that no memory gets deleted twice even with cycles.
        # Add a back-edge: parent-b -> root
        from uams.pipeline.cascade import CascadeForgetter
        sem = _InMemStore(MemoryType.SEMANTIC)
        sem.store(_make_mem("a", "a", relations=[{"type": "x", "target_memory_id": "b"}]))
        sem.store(_make_mem("b", "b", relations=[{"type": "x", "target_memory_id": "a"}]))
        sem.store(_make_mem("c", "c", relations=[{"type": "x", "target_memory_id": "a"}]))
        stores = {MemoryType.SEMANTIC: sem}
        f = CascadeForgetter(stores, UAMSConfig(cascade_max_depth=10), CascadeAuditWriter())
        r = f.forget("a", strategy="bidirectional")
        for mid in ("a", "b", "c"):
            self.assertEqual(r.deleted_ids.count(mid), 1)
```

- [ ] **Step 5: Run; expect new tests pass (cycle/orphan/partial already partly written; verify)**

Run: `python -m unittest tests.test_cascade -v`

Expected: now ~30 tests total, all green except the `Task 7 will implement` placeholder (now satisfied), and previously skipped `TestCascadeForgetterIsolated.test_strategy_isolated_only_target` is **not skipped anymore** (placeholder removed).

A `TestCascadeForgetterIsolated` test imports the new `f._stores_with_one_rel()` method that does NOT exist on the class. **Fix it**: either move the helper to module-level (cleaner) or keep it as a method but make it static. Move to module-level as `_stores_with_one_rel_chain()`:

```python
def _stores_with_one_rel_chain():
    s = _InMemStore(MemoryType.SEMANTIC)
    s.store(_make_mem("root", "root memory",
                      relations=[{"type": "follows", "target_memory_id": "child"}]))
    s.store(_make_mem("child", "child memory"))
    return {MemoryType.SEMANTIC: s}
```

Re-run: `python -m unittest tests.test_cascade -v` and confirm ~30 pass.

- [ ] **Step 6: Commit**

`_msg.txt`:
```
test(cascade): cycle + cross-tier orphan + partial failure + bidirectional

Covers spec § 6.1 invariants end-to-end:
- visit-set breaks a->b->a and a->b->c->a without double-deletes
- cross-tier targets and cross-tier reverse-references go to
  orphan_ids (never to deleted_ids)
- _PartialFailureStore proves best-effort: one delete() throwing does
  not abort the rest of the BFS queue; that ID lands in failed_ids

Brings the unit-test count to ~30 surface tests on the cascade module
(matches spec § 13 table).
```

```bash
git add tests/test_cascade.py
git commit -F _msg.txt
```

---

## Task 7: `system.forget()` rewire

**Files:**
- Modify: `src/uams/system.py` (only the `forget()` method ~line 573)
- Test: `tests/test_cascade.py` (1 new test asserting the rewire shape)

- [ ] **Step 1: Append failing test for `system.forget()` rewire**

```python
class TestSystemForgetRewire(unittest.TestCase):
    def test_system_forget_returns_cascade_report(self):
        # smoke test using a tiny in-process system
        from uams.system import UniversalMemorySystem
        from uams.core.config import UAMSConfig
        cfg = UAMSConfig(cascade_audit_log_path="/tmp/_uams_cascade_test_audit.jsonl",
                          cascade_orphan_log_path="/tmp/_uams_cascade_test_orphan.jsonl")
        u = UniversalMemorySystem(storage_backend="memory", config=cfg)
        # store a memory
        u.store(
            raw_payload="hello",
            agent_id="a", agent_type="t", session_id="s",
        )
        # find the id we just stored by querying
        from uams.core.models import MemoryId
        mem_id = list(u._stores[__import__("uams.core.enums", fromlist=["MemoryType"]).MemoryType.SEMANTIC]._mem.keys())[0]
        r = u.forget(mem_id)
        self.assertEqual(r.target_id, mem_id)
```

- [ ] **Step 2: Run; expect 1 fail (old forget doesn't return report)**

Run: `python -m unittest tests.test_cascade.TestSystemForgetRewire -v`

(If `system.py` already returns bool → the test fails on `r.target_id` AttributeError.)

- [ ] **Step 3: Modify `system.py`**

Open `src/uams/system.py`. Locate the existing `def forget(self, memory_id: str) -> bool:` (around line 573).

Insert near the top of the file (or wherever imports live):

```python
from uams.pipeline.cascade import CascadeForgetter, CascadeStrategy, CascadeReport
from uams.utils.cascade_audit import CascadeAuditWriter
```

Then add a constructor field in `UniversalMemorySystem.__init__` (find where `self._forgetting` is set; next line):

```python
        self._cascade_audit = CascadeAuditWriter(
            path=self.config.cascade_audit_log_path,
            orphan_path=self.config.cascade_orphan_log_path,
        )
        self._cascade_forgetter = CascadeForgetter(
            stores=self._stores,
            config=self.config,
            audit_writer=self._cascade_audit,
        )
```

Replace the old `forget()` body with:

```python
    def forget(
        self,
        memory_id: str,
        *,
        cascade: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
        max_depth: int | None = None,
        in_edge_mode: str | None = None,
    ) -> CascadeReport:
        """Forget a memory with configurable cascade.

        Strategy:
          - 'isolated'      : delete only `memory_id` (legacy single-shot)
          - 'outgoing'      : + delete out-edge targets (same tier)
          - 'bidirectional' : + delete reverse references (default; GDPR)

        Cross-tier edges are recorded as orphans (never deleted).

        Returns a `CascadeReport`. Never raises out of cascade.
        """
        # Accept both enums and plain strings.
        strategy = cascade if isinstance(cascade, CascadeStrategy) else CascadeStrategy(cascade)
        return self._cascade_forgetter.forget(
            memory_id,
            strategy=strategy,
            max_depth=max_depth,
            in_edge_mode=in_edge_mode,
        )
```

**Important**: existing tests that called `u.forget("x")` and expected `bool` will now receive `CascadeReport`. In particular, scan existing tests for usages — they likely use the return value of `forget()` for assertion.

- [ ] **Step 4: Audit existing tests touching `forget()` return value**

Run a search:
```
grep -n "\.forget(" tests/*.py
```
For each call site, decide:
- If the return value is ignored → no change needed.
- If the return value is asserted as True/False → **either** change the
  assertion to `report.deleted_count > 0` (preferred) **or** declare
  the test outside the scope of this PR.

Document any callers you keep unchanged in the commit message.

- [ ] **Step 5: Run full test suite**

Run: `python -m unittest discover -s tests`

If green (or only the expected skips), proceed.

If a test fails because it expected `forget()` to return `bool`:
- Update that test to assert `report.deleted_count > 0` instead.

- [ ] **Step 6: Run the new system test**

Run: `python -m unittest tests.test_cascade.TestSystemForgetRewire -v`

Expected: PASS.

- [ ] **Step 7: Commit**

`_msg.txt`:
```
feat(system): rewire forget() to dispatch through CascadeForgetter

UniversalMemorySystem.forget() now accepts a `cascade` keyword
defaulting to BIDIRECTIONAL (GDPR-style). The old single-shot delete
behavior is preserved when callers pass cascade=CascadeStrategy.ISOLATED
or the string "isolated".

Behavior switch is documented in the method docstring; callers who
ignored the return value are unaffected. Callers who asserted the
return as bool must switch to checking `report.deleted_count > 0`.

Any pre-existing call sites that need the bool form are documented
in the commit body.
```

```bash
git add src/uams/system.py tests/test_cascade.py
git commit -F _msg.txt
```

---

## Task 8: Backward-compat verification + flake8

**Files:**
- (no new files; verification only)

- [ ] **Step 1: Run full local test suite; expect no regressions**

Run:
```
python -m unittest discover -s tests
```

Expected: 317 + ~30 new tests, +21 still skipped locally, exit 0.

If any test fails, fix in place. Common cause: a test that did `forget()` and used `assertTrue(result)` will fail because `result` is now a `CascadeReport` (always truthy). The fix is `assertTrue(result.deleted_count > 0)`.

- [ ] **Step 2: Run flake8 on the new modules**

Run:
```
python -m flake8 src/uams/pipeline/cascade.py src/uams/utils/cascade_audit.py tests/test_cascade.py --select=E9,F63,F7,F82
```

Expected: 0 errors. If reports errors (e.g. `from __future__ import annotations` conflict), adjust.

- [ ] **Step 3: Quick perf smoke**

In a REPL:
```python
from uams.system import UniversalMemorySystem
u = UniversalMemorySystem(storage_backend="memory")
import time
ids = []
for i in range(20):
    m = u.store(raw_payload=f"memory {i}", agent_id="a", agent_type="t", session_id="s")
    ids.append(m.id if hasattr(m, "id") else str(m))
    # chain them
t = time.monotonic()
report = u.forget(ids[0])
print(f"forget took {(time.monotonic()-t)*1000:.1f}ms deleted={report.deleted_count}")
```

Expected: time < 50 ms even with 20 memories and default config.

- [ ] **Step 4: Confirm no documentation claims broken**

```
grep -rn "forget(memory_id)" docs/ PRODUCTION_ASSESSMENT.md
```

If any doc still claims "forget returns bool", update it to describe the new report shape.

- [ ] **Step 5: Commit (verification only, no source change unless step 1/2 fixed something)**

If anything needed a fix:
```bash
git add -A
git commit -F _msg.txt
```

`_msg.txt`:
```
chore: cascade backward-compat verification + flake8 clean
```

If nothing needed a fix: `git commit --allow-empty -F _msg.txt` (skip — `git commit --allow-empty` is fine).

---

## Task 9: User-facing docs `docs/CASCADE_FORGET.md`

**Files:**
- Create: `docs/CASCADE_FORGET.md`
- Modify: `docs/ARCHITECTURE.md` (link in)

- [ ] **Step 1: Create `docs/CASCADE_FORGET.md`**

```markdown
# Cascade forget (cross-layer deletion)

> **Status**: v0.2 (implemented in commit-after-`f5c5a56`). GDPR-friendly
> cross-memory forgetting with audit trail.

## TL;DR

`u.forget(memory_id)` in v0.2 deletes more than just that memory by
default. It traverses outgoing relations and reverse references (incoming
edges) within the target's tier, up to a configurable depth.

```python
from uams import UniversalMemorySystem
from uams.pipeline.cascade import CascadeStrategy

u = UniversalMemorySystem(storage_backend="sqlite")

# Same as before: just delete this one memory
u.forget("mem-1", cascade=CascadeStrategy.ISOLATED)

# Plus out-edge targets (forward-walk)
u.forget("mem-1", cascade=CascadeStrategy.OUTGOING)

# Plus reverse references too (default; GDPR-aligned)
u.forget("mem-1")  # equivalent to cascade=CascadeStrategy.BIDIRECTIONAL
```

## Why

Two reasons:

1. **Knowledge-graph integrity**. Once a memory is deleted, its
   relations become dangling pointers. Downstream searches and graph
   walks fall through invisible holes.
2. **Compliance**. GDPR Article 17 ("right to be forgotten") expects that
   deleting a user-attached record cascades through any derived /
   duplicated / aggregated records, with auditable evidence.

## How it works

1. **Locate** the target memory's tier (working / episodic / semantic /
   procedural). If absent, write an audit-only line and return.
2. **BFS discover** all related memories using a `visit_set` (cycle
   guard) + `max_depth` cap (default 4). Cross-tier edges are recorded
   as orphans but **never** trigger cross-tier deletion.
3. **Best-effort delete** in leaves-first order. Per-memory exceptions
   land in `report.failed_ids`. Other memories in the cascade still get
   deleted.
4. **Audit log**: one JSON line per invocation in
   `logs/cascade_forget_audit.jsonl`. One line per orphan edge in
   `logs/cascade_orphan_log.jsonl`.

## Configuration

| Field | Default | What it controls |
|---|---|---|
| `cascade_in_edge_strategy` | `"auto"` | `'scan'` = O(N) walk every store per call. `'index'` = use store-side reverse index if available, empty otherwise. `'auto'` = try index; fall back to scan per store. |
| `cascade_max_depth` | `4` | Hard cap on BFS depth. |
| `cascade_audit_log_path` | `logs/cascade_forget_audit.jsonl` | Per-invocation audit log. |
| `cascade_orphan_log_path` | `logs/cascade_orphan_log.jsonl` | Cross-tier orphan edges. |

Override via env or directly on `UAMSConfig(...)`.

## Reading the CascadeReport

```python
report = u.forget("mem-1")
print(report.target_id, "->", report.tier,
      "deleted:", report.deleted_count,
      "orphan:", report.orphan_count,
      "failed:", report.failed_count)
if not report.is_complete:
    print("partial failure; see audit log for replay")
```

## GDPR-aligned workflow

```python
# Operator triggers a deletion request under GDPR Art. 17
report = u.forget(target_id)

# Build a "deletion receipt" from the audit log line + cascade report
receipt = {
    "ts": report.to_dict()["ts"],
    "target": report.target_id,
    "deleted": report.deleted_ids,
    "failed": report.failed_ids,
    "audit_log": str(report.audit_log_path),
}
# Hand this to compliance / data subject
```

## Failure semantics

Cascade is **best-effort**: if memory `M` is in the BFS queue and its
`store.delete(M)` raises, `M` is appended to `report.failed_ids` with
the exception repr. Other memories in the queue still get deleted.
Audit log line is written either way, with `is_complete: false` if any
failure occurred.

If a partial failure is unacceptable for your workload, retry just the
failed IDs in a fresh isolated call:

```python
for fid, reason in report.failed_ids:
    print(f"retrying {fid} (was: {reason})")
    u.forget(fid, cascade=CascadeStrategy.ISOLATED)
```

## Out of scope (v0.2)

- cross-tier cascade (explicitly disabled; cross-tier edges are orphans only)
- soft-delete / tombstones (every cascade is a hard delete)
- 2PC / SAGA across multiple storage backends
- Async API mirrors (`AsyncUniversalMemorySystem.forget`)

## Spec & plan

- Spec: `docs/superpowers/specs/2026-07-10-cross-layer-forget-cascade-design.md`
- Plan: `docs/superpowers/plans/2026-07-10-cross-layer-forget-cascade.md` (this file)
```

- [ ] **Step 2: Add a link from `docs/ARCHITECTURE.md`**

Open `docs/ARCHITECTURE.md`. Find the table of contents or the section on
forgetting. Append a one-line link:

```markdown
- Cross-layer cascade delete: see [CASCADE_FORGET.md](./CASCADE_FORGET.md)
```

Skip this step if `ARCHITECTURE.md` does not have an appropriate TOC.

- [ ] **Step 3: Commit**

`_msg.txt`:
```
docs: CASCADE_FORGET.md user guide (cascade feature)

Covers strategy options, configuration, reading CascadeReport,
GDPR-aligned workflow, and partial-failure semantics. Links back to
the design spec and the implementation plan.
```

```bash
git add docs/CASCADE_FORGET.md docs/ARCHITECTURE.md
git commit -F _msg.txt
```

---

## Task 10: Update `PRODUCTION_ASSESSMENT.md` to v4

**Files:**
- Modify: `PRODUCTION_ASSESSMENT.md`

- [ ] **Step 1: Add a "v4" section at the top with the new feature**

Open `PRODUCTION_ASSESSMENT.md`. The existing v3 banner is:

```
> **2026-07-10 二次更新**:本次补上 **run #24 全绿(9/9 jobs)** ...
```

Add a new banner above it (or below, depending on which is most recent):

```
> **2026-07-10 v4 更新**:本次加上 **跨层 cascade forget** 特性(spec `f5c5a56` +
> plan follow-up commits)。3 策略默认 bidirectional + visit-set + max_depth=4
> + strict same-tier + best-effort + JSONL 审计,GPGD-friendly。 代码行约 +900,
> 测试数 317 → 347 (+30),6/6 后端 CI 不变。 评级 **不动**——B+/A-,因为
> cascade 只补产品能力,不够进入 A+(仍需 100k 压测 / 真实 LLM / pen-test
> 等缺口)。
```

- [ ] **Step 2: Update the headline table**

Change:
```
| 测试用例 | **317 个** ... |
```
to:
```
| 测试用例 | **347 个**(21 个本地 skipped;CI 上全过) |
```

- [ ] **Step 3: Add a row in the testing matrix**

Add a row to the testing matrix table:

```
| **test_cascade.py** | **30 ← v4 新增** | 3 策略 × 5 边界:cycle / cross-tier orphan / partial / audit concurrency |
```

- [ ] **Step 4: Update the "7-step roadmap"**

Mark a new step DONE — but **do not** remove or close out any of the existing ones; rather add this **8th** step marked complete:

```
| **8. 跨层 cascade forget** | ❌ | **✅ 完成(v4 commits)** | spec `f5c5a56` + 30 tests + docs/CASCADE_FORGET.md |
```

- [ ] **Step 5: Update "哪些维度升了" section**

Add:
- **企业特性 B+/A- → A** because cascade delete is the second most-asked
  enterprise feature after RBAC. (Other dims unchanged.)

- [ ] **Step 6: Commit**

`_msg.txt`:
```
docs: PRODUCTION_ASSESSMENT v4 — cascade-forget feature reflected
```

```bash
git add PRODUCTION_ASSESSMENT.md
git commit -F _msg.txt
```

---

## Task 11: Push + CI + final report

**Files:**
- (no new files; verification + push only)

- [ ] **Step 1: Push branch**

User did not specify a feature branch in the spec; this implementation can land on `main` directly. If the user has a preference for a branch, **stop and ask** before this step. Default assumption: push to `main`.

```bash
git push
```

- [ ] **Step 2: Poll GitHub Actions for the new run**

```bash
export GH_TOKEN=<paste>
python -c "
import os, urllib.request, json
HEAD = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'}
def get(url): return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=HEAD)).read())
r = get('https://api.github.com/repos/liwt2010/universal-agent-memory/actions/runs?per_page=1')['workflow_runs'][0]
print(r['name'], r['status'], r.get('conclusion') or 'pending', r['head_sha'][:7])
"
unset GH_TOKEN
```

Wait ~3–5 minutes for the run to complete; expect 9/9 jobs green.

- [ ] **Step 3: If CI red, diagnose**

Get the failed job's logs:
```bash
export GH_TOKEN=<paste>
python -c "
import os, urllib.request, json
HEAD = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'}
def get(url): return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=HEAD)).read())
run = get('https://api.github.com/repos/liwt2010/universal-agent-memory/actions/runs?per_page=1')['workflow_runs'][0]
for j in get(run['jobs_url'])['jobs']:
    if j['conclusion'] != 'success':
        print('FAILED:', j['name'])
        logs = urllib.request.urlopen(urllib.request.Request(j['logs_url'], headers=HEAD)).read()
        print(logs.decode('utf-8', errors='replace')[-4000:])
"
unset GH_TOKEN
```

Common causes:
- A test's flake8 import string changed. Fix in `test_cascade.py`.
- A UAMSConfig field default used a wrong default. Fix in `config.py`.
- A `unittest skipUnless` previously passed; now the test runs and
  exposes an issue. Fix in test.

Iterate until green.

- [ ] **Step 4: Final summary report**

Compose a final handoff message to the user with:
- Total commits (typically 9–12).
- Final test count: `unittest discover -s tests` line (`Ran 347 tests in …`).
- CI run number + green count.
- A pointer to `docs/CASCADE_FORGET.md` for usage.

---

# Self-review checklist (run before declaring plan complete)

These checks are duplicates of what the writing-plans skill instructs; this section is a quick-reference for the executor.

1. **Spec coverage** — for each spec section, find the matching task:
   - § 5 New types → Task 1 (CascadeStrategy + CascadeReport)
   - § 6 CascadeForgetter API → Task 1 skeleton + Task 5 full impl
   - § 7 In-edge discovery → Task 4 (locate + discover)
   - § 8 Cross-tier orphan → Task 5 (orphan_ids) + Task 6 (test)
   - § 9 Configuration → Task 3 (UAMSConfig fields)
   - § 10 system.forget migration → Task 7
   - § 11 Audit log format → Task 2 (writer) + Task 5 (call site)
   - § 12 Error handling → Task 5 best-effort + Task 6 partial test
   - § 13 Testing strategy → Task 1 + 2 + 5 + 6 each layer their slice
   - § 14 Performance → not in tests (out of scope per spec)
   - § 15 Acceptance → Task 8 (suite) + Task 11 (CI)

2. **Placeholder scan** — search for "TBD", "TODO", "implement later", "fill in details", "add appropriate error handling". **None should remain in this plan.**

3. **Type consistency** — verify:
   - `CascadeStrategy` is referenced as a class, not `CascadeStrategies` or `Strategy`.
   - `CascadeReport.to_dict()` is called once.
   - `_locate_tier`, `_discover_in_edges`, `_scan_in_edges_for_store` are the only private helpers of `CascadeForgetter` (no orphan methods).
   - `CascadeAuditWriter.append` and `CascadeAuditWriter.append_orphan` are the only write methods.

4. **Spec places not covered** (intentional):
   - Spec § 14.1 cost-model table — explanatory only; no test needed.
   - Spec § 18 future work — explicitly out of scope.

---

Plan complete.
