# Cascade forget (cross-layer deletion)

> **Status**: v0.2 (implemented in commits after `d53227a`). GDPR-friendly
> cross-memory forgetting with audit trail.

## TL;DR

`u.forget(memory_id)` in v0.2 deletes more than just that memory by
default. It traverses outgoing relations and reverse references
(incoming edges) within the target's tier, up to a configurable depth.

```python
from uams import UniversalMemorySystem
from uams.pipeline.cascade import CascadeStrategy

u = UniversalMemorySystem(storage_backend="sqlite")

# Same as before: just delete this one memory
u.forget("mem-1", cascade=CascadeStrategy.ISOLATED)

# Plus out-edge targets (forward-walk within the same tier)
u.forget("mem-1", cascade=CascadeStrategy.OUTGOING)

# Plus reverse references too (default; GDPR-aligned)
u.forget("mem-1")  # equivalent to cascade=CascadeStrategy.BIDIRECTIONAL
```

## Why

Two reasons:

1. **Knowledge-graph integrity**. Once a memory is deleted, its
   relations become dangling pointers. Downstream searches and graph
   walks fall through invisible holes.
2. **Compliance**. GDPR Article 17 ("right to be forgotten") expects
   that deleting a user-attached record cascades through any derived /
   duplicated / aggregated records, with auditable evidence.

## How it works

1. **Locate** the target memory's tier (working / episodic /
   semantic / procedural). If absent, write an audit-only line and
   return.
2. **BFS discover** all related memories using a `visit_set` (cycle
   guard) + `max_depth` cap (default 4). Cross-tier edges are
   recorded as orphans but **never** trigger cross-tier deletion.
3. **Best-effort delete** in leaves-first order. Per-memory
   exceptions land in `report.failed_ids`. Other memories in the
   cascade still get deleted.
4. **Audit log**: one JSON line per invocation in
   `logs/cascade_forget_audit.jsonl`. One line per orphan edge in
   `logs/cascade_orphan_log.jsonl`.

## Configuration

| Field | Default | What it controls |
|---|---|---|
| `cascade_in_edge_strategy` | `"auto"` | `'scan'` = O(N) walk every store per call. `'index'` = use store-side reverse index if available, empty otherwise. `'auto'` = try index; fall back to scan per store. |
| `cascade_max_depth` | `4` | Hard cap on BFS depth (walks N hops from root). |
| `cascade_audit_log_path` | `logs/cascade_forget_audit.jsonl` | Per-invocation audit log. |
| `cascade_orphan_log_path` | `logs/cascade_orphan_log.jsonl` | Cross-tier orphan edges. |

Override via env or directly on `UAMSConfig(...)`.

## Reading the CascadeReport

`forget()` returns a `CascadeReport`:

```python
report = u.forget("mem-1")
print(report.target_id, "->", report.tier,
      "deleted:", report.deleted_count,
      "orphan:",  report.orphan_count,
      "failed:",  report.failed_count)
if not report.is_complete:
    print("partial failure; see audit log for replay")
```

`report.deleted_ids` lists ids actually removed (in dependency
order). `report.orphan_ids` is a list of `(orphan_id, parent_id)`
pairs that mark cross-tier edges that were not followed. `report.
failed_ids` is `(id, reason)` for memories that the BFS reached
but whose `store.delete()` raised.

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

Cascade is **best-effort**: if memory `M` is in the BFS queue and
its `store.delete(M)` raises, `M` is appended to
`report.failed_ids` with the exception repr. Other memories in
the queue still get deleted. The audit log line is written
either way, with `is_complete: false` if any failure occurred.

If a partial failure is unacceptable for your workload, retry
just the failed IDs in a fresh isolated call:

```python
for fid, reason in report.failed_ids:
    print(f"retrying {fid} (was: {reason})")
    u.forget(fid, cascade=CascadeStrategy.ISOLATED)
```

## Migration from v0.1

`forget(memory_id)` previously returned `bool`. v0.2 returns
`CascadeReport`. Callers that ignored the return value are
unaffected. Callers that asserted on `True` / `False` should
switch to:

```python
report = u.forget(memory_id)
if report.deleted_count == 0:
    ...  # not found
```

The default strategy changed too — `forget(memory_id)` with no
`cascade=` argument now cascades bidirectionally. To get the
v0.1 single-shot delete behavior, pass
`cascade=CascadeStrategy.ISOLATED` explicitly.

## Out of scope (v0.2)

- cross-tier cascade (explicitly disabled; cross-tier edges are
  orphans only)
- soft-delete / tombstones (every cascade is a hard delete)
- 2PC / SAGA across multiple storage backends
- Async API mirrors (`AsyncUniversalMemorySystem.forget`)
- Per-backend reverse index adapters (today, every store
  implements scan-mode via `list_all()` + filter; no backend
  ships an O(1) reverse index yet)

## Spec & plan

- Spec: `docs/superpowers/specs/2026-07-10-cross-layer-forget-cascade-design.md`
- Plan: `docs/superpowers/plans/2026-07-10-cross-layer-forget-cascade.md`
