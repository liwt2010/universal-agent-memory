# Vault → UAMS Migration Guide (v0.3.x → v0.5.0)

> **Audience**: Vault maintainers integrating with `universal-agent-memory`.
> **TL;DR**: Every monkey-patch in Vault that wraps a UAMS API call with
> `try/except TypeError` can now be deleted. v0.4.0 added four public
> methods that do exactly what Vault has been doing in user space, but
> faster and safer. v0.5.0 closed related attack surfaces.

This guide is **not** about how to use UAMS from scratch. It assumes
you already have Vault code that talks to a UAMS instance and you want
to know which old patterns to drop and which new public methods to call
instead.

---

## 1. `ConsolidateResult` — `system.py:48`, exported at `uams.__init__`

### Before (v0.3.x)

```python
# Vault: peeking private state to count events
result = ums.consolidate(session_id=sid)            # returned None
session_events = ums._session_events.get(sid, [])   # private dict
source_count = len(session_events)
facts = []
for ev in session_events:
    if ev.structured_data and "fact" in ev.structured_data:
        facts.append(ev.structured_data["fact"])
episodic_id = session_events[0].structured_data.get("episodic_id")  # also private
```

This violates encapsulation:
- `ums._session_events` is a leading-underscore "private" attribute.
- Concurrency: `_session_events` is mutated by `observe()` under
  `_session_lock`, but reading from outside the lock is racy.
- `consolidate()` already cleared the buffer (`del self._session_events[session_id]`)
  by the time you get the count, so `len(session_events)` is always 0
  after a successful consolidate.

### After (v0.4.0+)

```python
from uams import ConsolidateResult   # public, re-exported at top level

result: ConsolidateResult = ums.consolidate(session_id=sid)
# All of these are atomic / post-condition stable:
print(result.source_event_count)        # int — number of events consolidated
print(result.episodic_memory_id)        # Optional[str] — resulting EPISODIC memory id
print(result.semantic_facts)            # int
print(result.procedural_patterns)       # int
print(result.duration_ms)              # float
print(result.error)                     # Optional[str] — None on success
```

Drop from Vault:
- `import` of any `uams.system._*` private symbol
- All `len(ums._session_events[sid])` calls
- Any "wait then read dict" pattern

---

## 2. `get_stats(scan_limit=...)` — `system.py:1047`

### Before (v0.3.x)

```python
# Vault: try/except TypeError to probe for the v0.4 kwarg
try:
    stats = ums.get_stats(scan_limit=10000)
except TypeError:
    # UAMS <0.4 doesn't accept scan_limit
    stats = ums.get_stats()
```

Why this is fragile:
- `TypeError` is a broad catch — it also fires if `stats` is shadowed
  by a `None`, a wrong import, or any other UAMS API breakage. A real
  bug gets swallowed as "old UAMS".
- It masks the failure mode when UAMS upgrades and the signature
  changes again.

### After (v0.4.0+)

```python
# v0.4.0 introduced the kwarg with a default of 1000. Drop the try/except.
stats = ums.get_stats(scan_limit=10000)
# scan_limit is keyword-only and clamps the reported count per tier so a
# misconfigured backend cannot return an unbounded number.
```

Optional defensive code (only if Vault must support UAMS <0.4 too):

```python
import inspect
if "scan_limit" in inspect.signature(ums.get_stats).parameters:
    stats = ums.get_stats(scan_limit=10000)
else:
    stats = ums.get_stats()
```

But for v0.5.0 deployments, drop the conditional entirely.

---

## 3. `delete_by_project_id(project_id, tenant_id=None)` — `system.py:774`

### Before (v0.3.x)

```python
# Vault: O(N) scan + per-row delete, hit SQLITE_MAX_VARIABLE_NUMBER
# on SQLite, missed rows on Redis when SCAN cap was too low.
deleted = []
for tier_name in ("WORKING", "EPISODIC", "SEMANTIC", "PROCEDURAL"):
    store = ums._stores[MemoryType[tier_name]]
    for mem in store.list_all(limit=10000):           # O(N) on the wire
        if mem.context.project_id != pid:
            continue
        if tid is not None and mem.context.tenant_id != tid:
            continue
        store.delete(str(mem.id))                     # N round-trips
        deleted.append(str(mem.id))
return len(deleted)
```

Known failure modes:
- **SQLite**: `list_all(limit=10000)` clamps to 999 internally
  (SQLITE_MAX_VARIABLE_NUMBER), so rows beyond the first 999 are
  silently dropped → memory leak per project.
- **Redis**: SCAN with `count=100` is non-exhaustive — keys can be missed
  mid-iteration if the keyspace changes.
- **PostgreSQL / Neo4j**: works but is N round-trips per delete.

### After (v0.4.0+)

```python
# Single call. scan_limit is a server-side filter; tenant_id is optional.
n = ums.delete_by_project_id(project_id=pid, tenant_id=tid)
return n
```

This calls `MemoryStore.delete_by_filter("project_id", ...)` on each
tier, which is:
- SQLite: `DELETE FROM t_memories WHERE project_id = ?` (indexed column)
- PostgreSQL: same with `%s` parameterization + GIN/B-tree on context
- Neo4j: `MATCH (m:Memory {project_id: $value}) DELETE r, m`
- ChromaDB: `collection.delete(where={"project_id": value})`
- Redis: SCAN + per-key HGETALL filter
- InMemory: `dict.items()` filter under RLock

All implementations are O(matches), not O(table).

If you only want to delete from a specific tier, use the store directly:

```python
n = ums._stores[MemoryType.EPISODIC].delete_by_filter("project_id", pid)
```

`MemoryStore.delete_by_filter` is part of the public abstract interface.

---

## 4. `revoke_project(project_id, cascade=...)` — `system.py:762`

### Before (v0.3.x)

```python
# Vault: project deletion = clear memories + clear agent associations
# + clear project metadata. Three separate code paths.
for mem_id in vault_db.query("SELECT memory_id FROM project_memories WHERE project_id=?", [pid]):
    ums.forget(mem_id)                                # one at a time, N round-trips

# "Cascade delete through relations" — Vault reimplemented this in user
# space by walking relations manually because `ums.forget(memory_id)`
# only deleted the target.
```

### After (v0.4.0+)

```python
# Single call: delete all memories with this project_id across all
# tiers, with a configurable cascade strategy.
report_count = ums.revoke_project(pid)
return report_count
```

Or, if you want the same structured output as `forget()`:

```python
# For per-memory cascade with relations, forget() still returns
# CascadeReport and is the right primitive:
report: CascadeReport = ums.forget(mem_id, cascade=CascadeStrategy.BIDIRECTIONAL)
print(report.deleted_ids, report.failed_ids, report.is_complete)
```

`revoke_project` is the bulk version; `forget` is the per-memory version.
Don't try to wrap `forget()` in a loop when you want bulk deletion.

For agent-level cleanup, the symmetric method exists:

```python
n = ums.revoke_agent(agent_id)        # bulk delete by agent_id
```

---

## 5. RateLimiter — `utils/security.py`

v0.3.x shipped a `RateLimiter` whose `is_allowed()` had a check-then-
append race under concurrency (the audit flagged this as P2 #13 but
didn't fix it at the time). v0.5.0 fixed it by adding a `threading.Lock`.

### Action for Vault

Nothing to change. If Vault holds its own RateLimiter instance and
calls it concurrently, the fix is automatic — same import path, same
API. The change is purely a tightening of the internal check-then-append
window.

If Vault had been catching the race by serializing calls itself
(`with vault_lock: rate_limiter.is_allowed(key)`), that wrapping can
now be removed.

---

## 6. `Memory.to_json` / `from_json` — round-trip `embedding` + `relations` in v0.4.1 (v0.5.0)

### Before (v0.4.0 and earlier)

```python
# Vault: backup JSON contained raw + structured + provenance
# but NO embedding and NO relations. After restore:
restored = Memory.from_json(data)
assert restored.payload.embedding is None   # silently lost
assert restored.metadata.relations == []   # silently lost

# Cascade-forget after restore would not discover in-edges from the
# restored relations list, breaking GDPR Article 17 traversal.
```

### After (v0.5.0)

```python
restored = Memory.from_json(data)
assert restored.payload.embedding == original_embedding   # preserved
assert restored.metadata.relations == original_relations # preserved
```

`embedding` round-trip is also now **strictly JSON-only** — legacy
pickle-encoded blobs in the backup store will be rejected with an
ERROR log and treated as `None`. Migration is a one-off:

```python
import pickle
from uams.utils.embedding_serde import serialize_embedding

for mid, blob in rows_with_pickle_embeddings:    # query: blob LIKE '\\x80%'
    try:
        data = pickle.loads(blob)                 # one-time only
        new_blob = serialize_embedding(data)
        # UPDATE ... SET embedding_blob = new_blob WHERE id = mid
    except Exception:
        log.warning("could not migrate embedding for %s", mid)
```

---

## 7. `AgentContext.tenant_id` — added in v0.5.0

`AgentContext` gained a `tenant_id` field for multi-tenant isolation.
The `delete_by_project_id(project_id, tenant_id=...)` API (already in
v0.4.0) reads from this field.

### Action for Vault

When constructing an `AgentContext`, set `tenant_id` explicitly for
multi-tenant deployments:

```python
ctx = AgentContext(
    agent_id=agent.id,
    agent_type="vault-agent",
    session_id=sid,
    user_id=user.id,
    project_id=proj.id,
    tenant_id=tenant.id,        # new field; required for multi-tenant safety
)
```

For single-tenant deployments the field stays `None` and behaviour is
unchanged.

---

## 8. Removed: `InputValidator.sanitize_sql`

v0.5.0 removed the keyword-based SQL denylist (it was an anti-pattern).
Any code calling it should be replaced with parameterised queries
(which UAMS storage layers already use internally) or with the new
whitelist helper:

```python
# Removed — replace with parameterised queries or:
if not InputValidator.is_safe_identifier(value):
    raise ValueError(f"unsafe identifier: {value!r}")
```

If Vault has been calling `sanitize_sql()` on free-text user input
before storing it, that input now goes straight into the storage layer
unchanged. Storage backends use parameterised queries, so injection
isn't possible regardless of what the input contains. The
sanitize_sql call was always redundant — its removal is purely cleanup.

---

## Migration checklist

For each pattern below, search Vault for the v0.3.x idiom and replace
with the v0.5.0 API:

| Old pattern in Vault | Replace with | Verified in |
|---|---|---|
| `ums._session_events.get(sid, [])` | `result.source_event_count` after `ums.consolidate(sid)` | `test_consolidate_result.py` |
| `try: ums.get_stats(scan_limit=...) except TypeError: ums.get_stats()` | `ums.get_stats(scan_limit=...)` | `test_revoke_and_count.py::test_get_stats_scan_limit_caps_count` |
| `for mem in store.list_all(10000): if mem.context.project_id == pid: store.delete(...)` | `ums.delete_by_project_id(pid, tenant_id=...)` | `test_revoke_and_count.py::TestDeleteByProjectId` |
| `for mem_id in project_memory_ids: ums.forget(mem_id)` (project-level cleanup) | `ums.revoke_project(pid)` | `test_revoke_and_count.py::TestRevokeProject` |
| `for mem_id in agent_memory_ids: ums.forget(mem_id)` (agent-level cleanup) | `ums.revoke_agent(agent_id)` | `test_revoke_and_count.py::TestRevokeAgent` |
| `with vault_lock: rl.is_allowed(key)` (workaround for rate-limiter race) | drop the wrapping — `RateLimiter` is now thread-safe | `test_aplus.py::test_rate_limiter_thread_safety` |
| Reading `memory.payload.embedding is None` after restore | real value preserved now | `test_embedding_serde.py` |
| `InputValidator.sanitize_sql(text)` | drop the call (parameterised queries at the storage layer handle it) | — |
| `AgentContext(..., tenant_id=None)` default | explicit `tenant_id` for multi-tenant deployments | `test_aplus.py::TestIsSafeIdentifierUsage` |

---

## Migration order recommendation

1. **First** — replace `delete_by_project_id` and `revoke_*` patterns.
   These are the highest-volume operations and have the worst performance
   cliff (SQLITE_MAX_VARIABLE_NUMBER → silent data loss).
2. **Second** — replace the `get_stats` `try/except TypeError`. Low risk,
   high readability win.
3. **Third** — switch `consolidate` consumers to `ConsolidateResult`.
   Mostly mechanical; tests cover the migration.
4. **Last** — drop the `sanitize_sql` calls. Pure cleanup, no behaviour
   change.

After each step, run Vault's own integration tests against UAMS v0.5.0.
The UAMS test suite itself is at 488 / 0 errors / 3 pre-existing failures
on every commit since `be5240a`.

---

## What UAMS guarantees for Vault

If Vault migrates to v0.5.0 APIs and stops reaching into `_`-prefixed
state, the following protections now hold:

- **No more silent SQLITE truncation**: `delete_by_project_id` uses
  indexed column DELETE, not the broken `list_all(limit=999999)` pattern.
- **No more RCE via pickle embeddings**: `embedding_serde` rejects
  legacy pickle blobs with an ERROR log instead of executing them.
- **No more GDPR cascade blind spots after restore**: `Memory.to_json`
  now round-trips `relations`, so cascade-forget sees the in-edges of a
  restored backup.
- **No more rate-limiter bypass under load**: `RateLimiter.is_allowed`
  holds the lock across the check-then-append.
- **No more DDL injection via project_id**: `UAMSConfig.validate`
  rejects unsafe `postgresql_table` values before any DDL runs.

These guarantees are enforced **at the UAMS layer** rather than
papered over at the Vault layer. That's the right place for them — the
moment someone else builds a second service on top of UAMS, they get
the same protections without having to replicate Vault's monkey-patches.
