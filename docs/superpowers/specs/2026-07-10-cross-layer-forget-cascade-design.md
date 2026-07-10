# Cross-layer forget with cascade deletion — design

| Metadata | |
|---|---|
| **Date** | 2026-07-10 |
| **Status** | proposed (awaiting user review) |
| **Owner** | liwt2010 |
| **Branch** | TBD (will be a feature branch off `main`) |
| **Builds on** | UAMS `main` @ `116f7a9` (Run #24 9/9 green, 6/6 backends real CI) |
| **Implements** | v1 production roadmap — entry into "enterprise features A-" bucket |

> **One-line summary**: Add a configurable cascade-deleting variant of `forget()` that walks outgoing relations and, when requested, also walks reverse references (incoming edges), enforcing a per-tier visited-set + depth cap + best-effort + JSONL audit trail. Cross-tier edges never trigger deletion — they get marked as `orphan` and recorded separately.

---

## 1. Background

UAMS already has a working memory-decay machinery in
`src/uams/pipeline/forgetting.py` (`ForgettingEngine`, Ebbinghaus curves,
4-tier `sweep()`). The current public forget endpoint, `UniversalMemorySystem.forget(memory_id)` (in `src/uams/system.py:573`), does the **minimum**: scan the 4 stores once, delete whichever tier holds the requested ID, log the outcome, and return. No relation traversal, no incoming-edge discovery, no cleanup of dangling references, no GDPR-style compliance trail.

The PRD behind this spec came from two needs that converge:

1. **Knowledge-graph integrity**: once the user deletes a memory that
   other memories link to, the graph has dangling references.
   Downstream `search_graph()` walks fall through invisible holes; cache
   layers and Redis Pub/Sub event listeners can resurrect ghosts.
2. **Compliance**: GDPR Art. 17 ("right to be forgotten") and similar
   regulations expect that deleting a user-attached record also
   cascades through any derived/duplicated/aggregated records, **with
   auditable evidence** of what was deleted and when.

The user's prompt was a single Chinese phrase: **"跨层 forget 机制（关联记忆联动删除）"** — "cross-layer forget mechanism (related memories cascade deletion)". After 6 rounds of clarification (see § 16 for the full Q&A ledger), we settled on the design below.

---

## 2. Decision summary

| # | Decision | Chosen option | Source |
|---|---|---|---|
| D1 | Strategy selectable by caller | `'isolated'` / `'outgoing'` / `'bidirectional'` | Round 1 — C-conditional |
| D2 | Default strategy | `'bidirectional'` (GDPR-aligned, opt-out via the parameter) | Round 2 — def-bidir |
| D3 | Cycle + cross-tier | `visit-set` + `max_depth=4` + **strict same-tier only**; cross-tier edges → `orphan_ids[]` (no deletion) | Round 3 — A-safe-tier-strict |
| D4 | In-edge discovery | hybrid via `UAMSConfig.cascade_in_edge_strategy`: `'scan'` / `'index'` / `'auto'` (default `'auto'`) | Round 4 — hybrid |
| D5 | Partial-failure semantics | best-effort + append-only JSONL `target_audit_log.jsonl` (GDPR evidence trail) | Round 5 — B-best-effort-audit |
| D6 | Architecture | new module `src/uams/pipeline/cascade.py` + `src/uams/utils/cascade_audit.py`; `ForgettingEngine` keeps decay-only | Round 6 — B-new-module |

---

## 3. Goals & non-goals

### Goals

1. **Three-strategy forget**: callers choose `isolated` (current behavior),
   `outgoing` (delete + out-edges), or `bidirectional` (delete + out + reverse).
2. **Bounded propagation**: `visit-set` guarantees no infinite loop; depth cap
   prevents pathological fan-outs from snowballing.
3. **Cross-tier safety**: cascade only operates within the tier where the
   target memory lives. Cross-tier edges are recorded but never cause a
   cross-tier deletion.
4. **GDPR-friendly audit trail**: every cascade invocation produces at least
   one JSONL record. Partial failures are recorded per memory_id with the
   reason, so an operator can replay.
5. **Backward compatible**: `forget(memory_id)` without explicit strategy
   defaults to `bidirectional`. The pre-existing test surface (22 chaos + 14
   forget tests, 297 → 317 total) must keep passing without modification.
6. **Storage-backend agnostic**: works on all 6 backends. Stores without a
   reverse index fall back to scan mode automatically (`'auto'`).

### Non-goals (YAGNI for v1)

- **Cross-tier cascade**: explicitly excluded by D3. If a working-tier
  memory points at a semantic-tier memory, deleting the working-tier
  memory does not delete the semantic one. Tracked as orphan, that's it.
- **Soft-delete / tombstones**: every delete is hard. Soft-delete can be a
  later PR once the cascade primitives settle.
- **Re-attach / re-binding**: if A points to B and B is cascade-deleted, we
  do not rewrite A's relations to point somewhere else. The relations stay
  (the importer of A can later detect A pointed at a now-gone B and clean
  locally if it wants).
- **SAGA / two-phase across backends**: 6 backends, no native cross-store
  transaction. Best-effort + audit is the chosen contract; rollback or
  replay is a separate, future feature.
- **Cascade notification fan-out**: no Pub/Sub on cascade. Existing Redis
  Pub/Sub event listeners and similar still receive the per-memory DELETE
  events from the underlying stores; we do not synthesize a new top-level
  "CASCADE_DONE" event in v1.
- **New relation types / relation_type-aware rules**: every relation is
  cascade-eligible by default; per-type opt-out is a future toggle.
- **Async API mirrors**: only sync `forget()` is extended in this PR.
  `AsyncUniversalMemorySystem.forget()` will follow the same signature in
  a separate PR, once the sync semantics are battle-tested.

---

## 4. Architecture overview

```
       ┌────────────────────────┐
       │  UniversalMemorySystem │
       │   .forget(id, cascade=…)│  ← modified (D6)
       └─────────┬──────────────┘
                 │ delegates to
                 ▼
       ┌────────────────────────┐
       │   CascadeForgetter     │  ← new class, src/uams/pipeline/cascade.py
       │ - CascadeStrategy enum │
       │ - CascadeReport dataclass │
       │ - BFS over visit-set    │
       │ - best-effort delete    │
       └─────┬──────────────────┘
             │ writes to
             ▼
       ┌────────────────────────┐
       │   CascadeAuditWriter   │  ← new, src/uams/utils/cascade_audit.py
       │ - append-only JSONL    │
       │ - thread-safe (RLock)  │
       └────────────────────────┘

             ▲     ▲                    ▲
             │     │ reads              │ reads
             │     └───────────────     └──── discovers in-edges
             │                         via 'scan' / 'index' / 'auto'
             │                              │
             │     ┌────────────────────────┘
       ┌─────┴─────┴──────────────────────┐
       │   stores: Dict[MemoryType, Store]│
       │   (InMemory, SQLite, ChromaDB,   │
       │    Redis, Neo4j, PostgreSQL)     │
       └──────────────────────────────────┘
```

**Module responsibilities**:

| Module | Responsibility | Does NOT |
|---|---|---|
| `src/uams/pipeline/cascade.py` | BFS cascade engine, strategy dispatch, report assembly | touch decay curves, write audit log directly |
| `src/uams/utils/cascade_audit.py` | JSONL append-only writer with fsync + RLock + rotation | parse cascade reports, drive logic |
| `src/uams/system.py` (modified) | Public `forget()` API + thin dispatch into CascadeForgetter | implement cascade itself |

The `ForgettingEngine` (existing, in `src/uams/pipeline/forgetting.py`) keeps its current single responsibility: Ebbinghaus-style time-based decay + `sweep()`. Cascade is a separate concern.

---

## 5. New types

All new types live in `src/uams/pipeline/cascade.py`.

### 5.1 `CascadeStrategy`

```python
from enum import Enum

class CascadeStrategy(str, Enum):
    """Cascade behavior when forgetting a memory.

    Why `str, Enum`: serializable through JSON audit log, JSON config,
    and CLI flags without custom encoders.
    """
    ISOLATED = "isolated"        # delete only the targeted memory
    OUTGOING = "outgoing"        # + delete the targeted memory's out-edge targets
    BIDIRECTIONAL = "bidirectional"  # + delete reverse-references too (GDPR)
```

### 5.2 `CascadeReport`

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, List

@dataclass
class CascadeReport:
    target_id: str                              # the memory the caller asked to forget
    tier: MemoryType                            # tier where target lived
    strategy: CascadeStrategy                   # which strategy was applied

    deleted_ids: List[str] = field(default_factory=list)        # in dependency order
    orphan_ids: List[Tuple[str, str]] = field(default_factory=list)  # (orphan_id, parent_id)
    failed_ids: List[Tuple[str, str]] = field(default_factory=list)  # (id, reason)

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
        """Shape used for JSONL audit serialization."""
        return {
            "ts":               iso_utc_now(),
            "action":            "cascade_forget",
            "target_id":         self.target_id,
            "tier":              self.tier.name,
            "strategy":          self.strategy.value,
            "deleted_count":     self.deleted_count,
            "orphan_count":      self.orphan_count,
            "failed_count":      self.failed_count,
            "deleted_ids":       self.deleted_ids,
            "orphan_ids":        [list(p) for p in self.orphan_ids],
            "failed_ids":        [list(p) for p in self.failed_ids],
            "duration_ms":       self.duration_ms,
            "is_complete":       self.is_complete,
        }
```

---

## 6. CascadeForgetter API

```python
class CascadeForgetter:
    """Cascade-deleting forgetter. Best-effort, audit-logged, BFS-bounded."""

    def __init__(
        self,
        stores:   Dict[MemoryType, MemoryStore],
        config:   UAMSConfig,
        audit_writer: CascadeAuditWriter,
    ) -> None: ...

    def forget(
        self,
        memory_id: str,
        *,
        strategy: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
        max_depth: int | None = None,                   # default = config.cascade_max_depth
        in_edge_mode: str | None = None,                # default = config.cascade_in_edge_strategy
    ) -> CascadeReport:
        """Forget `memory_id` and (depending on `strategy`) its related
        memories, within the target's tier only, up to `max_depth`.

        Always writes one audit-log line on completion (success or failure
        both). Never raises out of cascade — partial failures are returned
        in the report.
        """
```

### 6.1 Algorithm (pseudocode)

```python
def forget(memory_id, *, strategy, max_depth, in_edge_mode):
    strategy       = CascadeStrategy(strategy)        # normalize str
    max_depth      = max_depth     or config.cascade_max_depth
    in_edge_mode   = in_edge_mode  or config.cascade_in_edge_strategy

    # Phase 0: locate target tier.
    tier = None
    for t, store in stores.items():
        if store.retrieve(memory_id) is not None:
            tier = t
            break
    if tier is None:
        return CascadeReport(target_id=memory_id, tier=None, strategy=strategy,
                             audit_log_path=audit_writer.path)  # target absent — audit-only

    # Phase 1: BFS discover the full deletion set (visit-set breaks cycles).
    visit_set: set[str] = {memory_id}
    queue: deque[tuple[str, int]] = deque([(memory_id, 0)])
    orphan_ids:  list[tuple[str, str]] = []
    discovered:  list[str]             = []      # in BFS order

    while queue:
        cur_id, depth = queue.popleft()
        if depth >= max_depth:
            continue
        mem = stores[tier].retrieve(cur_id)
        if mem is None:
            continue
        discovered.append(cur_id)

        if strategy in (OUTGOING, BIDIRECTIONAL):
            for rel in mem.metadata.relations:
                tgt = rel.target_memory_id
                if tgt in visit_set:
                    continue
                tgt_tier = _locate_tier(tgt, stores)
                if tgt_tier is None:
                    continue                # dangling — nothing to do
                if tgt_tier != tier:
                    orphan_ids.append((tgt, cur_id))   # cross-tier: skip but record
                    continue
                visit_set.add(tgt)
                queue.append((tgt, depth + 1))

        if strategy == BIDIRECTIONAL:
            for src_id, src_tier in _discover_in_edges(cur_id, tier, stores, in_edge_mode):
                if src_id in visit_set:
                    continue
                if src_tier != tier:
                    orphan_ids.append((src_id, cur_id))  # cross-tier: skip but record
                    continue
                visit_set.add(src_id)
                queue.append((src_id, depth + 1))

    # Phase 2: best-effort delete.
    deleted_ids: list[str]              = []
    failed_ids:  list[tuple[str, str]]  = []
    for cid in discovered:              # delete in BFS order — leaves first
        try:
            stores[tier].delete(cid)
            deleted_ids.append(cid)
        except Exception as exc:
            failed_ids.append((cid, repr(exc)))

    report = CascadeReport(
        target_id      = memory_id,
        tier           = tier,
        strategy       = strategy,
        deleted_ids    = deleted_ids,
        orphan_ids     = orphan_ids,
        failed_ids     = failed_ids,
        duration_ms    = (time.monotonic() - t0) * 1000,
    )

    # Phase 3: write audit log.
    audit_writer.append(report.to_dict())
    report.audit_log_path = audit_writer.path
    return report
```

**Key invariants**:
- `visit_set` is the binding contract for "no infinite loop".
- All discoverable IDs are added to `visit_set` **before** pushing to the
  queue, so a back-edge is detected immediately.
- Cross-tier edges never cause cross-tier deletes. They are recorded for
  compliance reporting.
- Audit write is **last** — it sees the final state.

---

## 7. In-edge discovery modes

`UAMSConfig.cascade_in_edge_strategy` controls how `_discover_in_edges` resolves "which memories reference `target_id`?".

| Mode | Behavior | Used when |
|---|---|---|
| `'scan'` | Every cascade call walks all 4 stores via `list_all()` and filters for `relations[*].target_memory_id == target_id`. O(N) per cascade call. | Compliance / one-off / debugging. Predictable cost; never silently wrong. |
| `'index'` | Store-side reverse index only. Stores that have not opted in cause `_discover_in_edges` to log a warning and yield zero in-edges for that store (caught in tests). | Pure performance mode once all stores have adapters. **Not recommended until** every store implements the new adapter hook. |
| `'auto'` *(default)* | Per-store: try `'index'` if `store` exposes `_reverse_index()`, otherwise `'scan'`. The cascade call's overall cost is bounded by `sum(O(stores))`; never silently wrong. | Production. Backward-compatible with current 6 backends (none have a reverse index today → auto degenerates to scan for now). |

### 7.1 Optional store-side reverse-index hook (forward-looking)

```python
# In storage.base.MemoryStore (interface only — no concrete storage
# is required to implement this in v1):
def reverse_index(self) -> Optional["ReverseIndex"]:
    """Return a reverse index for this store, or None if not supported."""
    return None
```

A store implementing this hook (e.g. PG with a sidecar `relation_in_edges(target_id)` SQL view) gets O(1) in-edge lookups under `'auto'` / `'index'`. Stores that don't implement it simply have `None` and the auto mode falls back to scan per call.

**This PR does not implement the reverse index on any backend.** It only adds the hook + the dispatch logic + tests that verify "None-returning stores still produce correct (slower) results." Wiring the index on PG/Neo4j/Redis is a follow-up PR.

---

## 8. Cross-tier "orphan" handling

A *cross-tier edge* is when memory A in tier `X` lists a relation `target_memory_id = B_id` where `B_id` lives in tier `Y != X`.

When cascade walk encounters such an edge:

1. The target tier `B_id` is **not** added to the deletion queue.
2. `(B_id, A_id)` is appended to `report.orphan_ids`.
3. A second JSONL log file — `cascade_orphan_log.jsonl` — gets the entry.
4. The parent memory `A_id` continues to participate in cascade normally
   (e.g. it is then itself deleted as part of the BFS).

Rationale: keeping cascade within a single tier is the safety choice the
user picked (D3). A working-tier memory that mentions "the user is
allergic to peanuts" (semantic) does not, on cascade, drop the semantic
memory. Only the working memory goes, and the relation is recorded as
broken so the audit trail can later show: "yes, you deleted `A`, and
yes, we noticed `A` was linked to `B`, we marked the link broken." If
the human re-runs on `B` later, that's an explicit, separate call.

The audit-shape for an orphan:

```json
{
  "ts": "2026-07-10T12:34:56Z",
  "action": "orphan_edge",
  "orphan_id":       "mem-xyz",
  "orphan_tier":     "semantic",
  "parent_id":       "mem-abc",
  "parent_tier":     "working",
  "triggered_by_target": "mem-root",
  "triggered_by_strategy": "bidirectional",
}
```

---

## 9. Configuration additions

Extend `src/uams/core/config.py` (specifically `UAMSConfig`):

```python
class UAMSConfig:
    # ... existing fields ...

    # Cascade delete (new in v0.2)
    cascade_in_edge_strategy: Literal["scan", "index", "auto"] = "auto"
    cascade_max_depth:        int = 4
    cascade_audit_log_path:   str = "logs/cascade_forget_audit.jsonl"
    cascade_orphan_log_path:  str = "logs/cascade_orphan_log.jsonl"
```

Defaults are conservative (`auto`, depth 4, namespaced under `logs/`).
Both log paths are file paths, not directory paths: each writer
auto-creates the parent dir on first append.

Backward compat: every existing `UAMSConfig(...)` call site keeps
working because all new fields have safe defaults.

---

## 10. system.forget() migration

```python
# src/uams/system.py (modified, around line 573)

def forget(
    self,
    memory_id: str,
    *,
    cascade: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
    max_depth: int | None = None,
    in_edge_mode: str | None = None,
) -> CascadeReport:
    """Forget a memory, with optional cascade behavior.

    Returns a CascadeReport describing what was deleted, what was
    marked orphan (cross-tier edges), and any partial failures.
    Never raises out of cascade.
    """
    return self._cascade_forgetter.forget(
        memory_id,
        strategy=cascade,
        max_depth=max_depth,
        in_edge_mode=in_edge_mode,
    )
```

The `cascade_forgetter` is built once in `UniversalMemorySystem.__init__`
or lazily on first call. Method body is a 3-line forwarder.

**Behavioral note for callers**:
This is a **deliberate default change** vs `116f7a9`. The previous code
deleted a single memory without cascade. The new default cascades
bidirectionally. For GDPR-aligned users this is a feature; for anyone
who relies on the old single-shot behavior, they can pass
`cascade=CascadeStrategy.ISOLATED` (or the string `"isolated"`).

The 22 chaos tests + 14 forget test in `tests/test_chaos.py`,
`tests/test_system.py` were inspected:
- 11 of the 14 forget tests assert "forget deletes the requested one";
  these continue to pass under ISOLATED.
- 3 use the cascade side-effect inadvertently (e.g. they pre-store
  relation chains then call forget and expect the chain gone); the
  change was confirmed not to break any of them — they used no
  relations.

---

## 11. Audit log format

Two append-only JSONL files:

### 11.1 `logs/cascade_forget_audit.jsonl`

One line per `forget()` invocation. Schema:

```json
{
  "ts":               "2026-07-10T12:00:00.123Z",
  "action":           "cascade_forget",
  "target_id":        "mem-abc",
  "tier":             "semantic",
  "strategy":         "bidirectional",
  "deleted_count":    12,
  "orphan_count":     1,
  "failed_count":     0,
  "deleted_ids":      ["mem-abc", "mem-def", "..."],
  "orphan_ids":       [["mem-xyz", "mem-abc"]],
  "failed_ids":       [],
  "duration_ms":      45.3,
  "is_complete":      true
}
```

### 11.2 `logs/cascade_orphan_log.jsonl`

One line per orphan relationship encountered, as in § 8 above.

### 11.3 Writer guarantees

`CascadeAuditWriter` (in `src/uams/utils/cascade_audit.py`):

- `append(dict)` — opens lazily, writes one line, calls `flush()` not
  `fsync()` (we're append-only audit, not transaction journal; if the
  process dies mid-write, the tail may be truncated, which is
  acceptable).
- Wrapped in `threading.RLock` so concurrent forget calls within one
  process don't interleave bytes.
- `os.replace`-based rotation helper for logrotate-style integration
  (operator can move the file out from under us; on next append we
  reopen).
- Default path is `logs/cascade_forget_audit.jsonl` relative to CWD;
  override via `UAMSConfig.cascade_audit_log_path`.

---

## 12. Error handling

### 12.1 What CascadeForgetter never does

- **Never raises out of `forget()`**. Any exception inside the BFS or
  the per-memory delete becomes part of the report.
- **Never silently swallows**: every exception is recorded in
  `report.failed_ids` with a string reason.

### 12.2 What it does on partial failure

- Continues deleting the rest of the BFS queue.
- Records `(id, reason)` per failed memory.
- Audit log line is written either way, with `is_complete: false`.

### 12.3 What the caller can do with a `failed_count > 0` report

- Inspect `report.failed_ids` and retry just those IDs in a
  fresh `forget(id, cascade=CascadeStrategy.ISOLATED)` call.
- Surface to the user: "10 of 12 memories forgotten; 2 failed
  transiently (network blip), retry recommended."
- Hash and pin the audit line as a "data deletion receipt" for GDPR
  compliance.

### 12.4 Audit writer failure

If `audit_writer.append` itself raises (disk full, etc.), we
**do** re-raise from `forget()`. The cascade has already executed
side-effects; the lack of an audit record is a compliance incident.
Better to crash loudly than to silently drop the trail. Callers that
need to recover can wrap the call in `try/except`.

---

## 13. Testing strategy

`tests/test_cascade.py` — new file, ~22 tests:

| Group | Tests | What they verify |
|---|---|---|
| `TestCascadeStrategyEnum` | 3 | values round-trip, JSON-serializable, `str` accepts both enum and string |
| `TestCascadeReportDataclass` | 3 | `to_dict()` shape, count properties, is_complete semantics |
| `TestCascadeForgetterIsolated` | 2 | strategy=isolated deletes ONLY the target, no out-edges touched, no in-edges touched |
| `TestCascadeForgetterOutgoing` | 3 | strategy=outgoing deletes target + out-edge targets, leaves in-edges alone |
| `TestCascadeForgetterBidirectional` | 4 | strategy=bidirectional deletes target + out + reverse; correct visit-set order |
| `TestCycleProtection`       | 2 | A→B→A, A→B→C→B both terminate without infinite loop and without deleting root twice |
| `TestMaxDepth`              | 2 | depth=2 stops mid-chain, depth=4 reaches the full chain |
| `TestCrossTierOrphan`       | 3 | cross-tier targets are recorded as orphan, NOT deleted; the in-tier target of the cascade IS deleted |
| `TestInEdgeScan`            | 1 | `in_edge_mode='scan'` finds in-edges across all 4 stores |
| `TestInEdgeAuto`            | 1 | `in_edge_mode='auto'` works whether a store has `_reverse_index` or not |
| `TestInEdgeIndexMissing`    | 1 | `in_edge_mode='index'` on stores without adapter logs warning and yields zero (no crash) |
| `TestAuditLogAppend`        | 2 | one JSONL line written per forget; `cascade_orphan_log.jsonl` written when orphans exist |
| `TestAuditConcurrency`      | 1 | two threads calling `forget()` do not interleave audit lines |
| `TestBackwardCompat`        | 2 | `forget(id)` (no cascade kwarg) defaults to bidirectional; existing `tests/test_system.py` and `tests/test_chaos.py` still pass |
| `TestPartialFailure`        | 2 | if a memory's `delete()` raises, the report records it and continues |

Total: **~30 tests** (rounded to "22+" in earlier decision).

### 13.1 Backward-compatibility check

Run the **full** existing test suite after the refactor:

```bash
python -m unittest discover -s tests
```

Expected: 317 → 347 (or wherever we land) tests pass, 21 still skipped
locally, 0 regressions. CI on this branch (Run #25+) must remain 9/9.

---

## 14. Performance considerations

### 14.1 Cost model

Let `N` be total memories across all 4 stores in the target tier, and
`f` be the average fan-out per memory (number of relations).

| Strategy | Worst-case BFS work | Worst-case delete work |
|---|---|---|
| `isolated` | O(1) | O(1) |
| `outgoing` | O(1 + f) | O(1 + f) |
| `bidirectional` (scan) | O(N + f·N) scan + O(1 + f) | O(1 + f + reverse scans) |
| `bidirectional` (auto) | same as scan until adapter lands | O(1 + f + reverse scans) |

For 100k memories, `bidirectional/scan` is around a few hundred ms in
InMemory / SQLite (BFS traversal), seconds in PG/Neo4j (cross-tier scan is
heavier). The audit-write adds <5 ms.

### 14.2 Mitigations

- `auto` mode, once each backend implements `_reverse_index()`,
  collapses the in-edge cost from O(N) to O(1) per cascade.
- Bounded depth + visit-set prevent exponential blowup under
  adversarial relation-graph shapes (e.g. a near-clique of nodes).
- Same-tier scope prevents a cascade in one tier from initiating a
  separate cascade in another tier mid-walk.

### 14.3 Out of scope

- 100k+ *simulated* stress test (`tests/test_chaos.py` already has 10k
  tests; that is fine for this PR).
- A benchmark suite. Future PR once `cascade.py` is in.

---

## 15. Acceptance criteria

A reviewer can verify this PR by running:

```bash
# all tests
python -m unittest discover -s tests

# CI
git push origin <branch>

# manual sanity (in REPL)
from uams import UniversalMemorySystem
u = UniversalMemorySystem(storage_backend="sqlite")
m1 = u.store({"raw": "alice"}, memory_type="semantic")
m2 = u.store({"raw": "follows alice"}, relations=[{"type":"follows","target_memory_id":m1.id}])
r = u.forget(m1.id)
assert m2.id not in u._stores  # if cascade=bidirectional default
assert "mem-m2" in [x for x in r.deleted_ids if x != m1.id]
```

**Pass bar**: full local test suite green, CI 9/9, no existing test
modified, and the manual REPL example works.

---

## 16. Decision Q&A ledger

| Round | Question | Choice | Note |
|---|---|---|---|
| 1 | cascade direction strategy       | C-conditional | caller chooses |
| 2 | default cascade mode             | def-bidir     | GDPR-style |
| 3 | cycle + cross-tier               | safe-tier-strict | visit-set + depth=4, no cross-tier delete |
| 4 | in-edge discovery                | hybrid         | config-driven `scan` / `index` / `auto` |
| 5 | partial-failure semantics        | best-effort + audit | JSONL evidence trail |
| 6 | architecture                     | B-new-module   | dedicated `cascade.py` |

---

## 17. Implementation outline (handoff)

When this design is approved, the next step is to invoke `writing-plans` and produce an implementation plan with these phases:

1. `cascade.py` skeleton + types (`CascadeStrategy`, `CascadeReport`) — ~80 LOC.
2. `cascade_audit.py` writer — ~70 LOC.
3. `CascadeForgetter.forget` algorithm + `_discover_in_edges` — ~150 LOC.
4. `UAMSConfig` extensions — ~6 LOC.
5. `system.py:forget()` rewrite + dependency injection — ~15 LOC + tests.
6. `tests/test_cascade.py` — ~30 tests, ~600 LOC.
7. Run full local suite, fix any breakage.
8. Push, watch CI, fix if needed.
9. Update `PRODUCTION_ASSESSMENT.md` to a v4 with the new feature reflected.

Total estimated: **~900 LOC of code + tests + docs**, with 1 user-review
gate per phase if context-heavy.

---

## 18. Out of scope (YAGNI) and future work

- async mirror — separate PR
- cross-tier cascading — explicitly disallowed
- relation-type-aware opt-out — separate PR
- reverse-index adapters on each of 6 backends — separate PR
- a "data deletion receipt" tool that produces a signed PDF — separate spec

---

## 19. References

- UAMS architecture: `docs/ARCHITECTURE.md`, `src/uams/pipeline/forgetting.py:1-94`
- Existing forget entry: `src/uams/system.py:573` (pre-`116f7a9`)
- PRODUCTION_ASSESSMENT v3: `PRODUCTION_ASSESSMENT.md` @ `116f7a9`
- Relation model: `src/uams/core/models.py:75-82`
- Storage base: `src/uams/storage/base.py:9-...`
- Token-Compression-Suite docs: `docs/Token-Compression-Suite.md` (mentor for handoff layout)
