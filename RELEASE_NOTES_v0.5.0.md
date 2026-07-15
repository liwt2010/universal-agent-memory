# v0.5.0 — Security Hardening (closing real attack surfaces)

**⚠️ Breaking release.** This release removes two historical "compatibility shims" that were themselves attack surfaces or anti-patterns. Operators with existing v0.3.x / v0.4.x deployments must read the migration notes below before upgrading.

## What changed

### Closed attack surfaces

- **`Memory.to_json` / `from_json` round-trip `embedding` + `relations`**. The previous implementation silently dropped both, breaking backup/restore vector search AND cascade-forget in-edge discovery after restore. This was a real data-loss bug, not theoretical. Now serialized.
- **`embedding_serde.deserialize_embedding` no longer falls back to `pickle.loads`**. Legacy pickle-encoded blobs are rejected with an explicit ERROR log and treated as `None`. This closes a remote-code-execution vector: an attacker who could write to a shared store (PostgreSQL, Redis, SQLite file) would previously get arbitrary Python execution on the next `retrieve()`. Migration script in the module docstring.
- **`UAMSConfig.validate()` now rejects unsafe identifiers and paths** before any DDL / Redis key / log-file operation runs. New constraints:
  - `postgresql_table` must match `[A-Za-z0-9_-]+`
  - `redis_key_prefix` / `redis_cache_key_prefix` must match `[A-Za-z0-9_:-]+`
  - `cascade_audit_log_path` / `cascade_orphan_log_path` must not contain NUL, `|`, or `;`
  - `cascade_max_depth` bounded to `0..8`
  - `cascade_in_edge_strategy` validated against `scan | index | auto`
- **`RateLimiter` is now thread-safe**. The previous check-then-append path was a race that could let through more than the configured `max_requests` under concurrent load. Added `threading.Lock`; regression test under 8×100 concurrent calls verifies exactly the configured number pass.

### Removed (anti-patterns / RCE vectors)

- **`InputValidator.sanitize_sql` removed**. Keyword denylists are a well-known anti-pattern: they give false confidence while missing real attacks (bypass via `||`, `&&`, mixed case, comments). UAMS already uses parameterised queries everywhere. Replaced by `InputValidator.is_safe_identifier(value)` — a whitelist grammar check for IDs that genuinely get interpolated into DDL or Redis key prefixes.

### Added

- **`AgentContext.tenant_id`** for multi-tenant isolation. Pairs with the v0.4.0 `delete_by_project_id(project_id, tenant_id=...)` API.

## Breaking changes — migration recipe

### 1. `InputValidator.sanitize_sql` is gone

Any caller using it should switch to:
- **For query safety**: parameterised queries (which UAMS already uses internally; this is the actual defence).
- **For DDL / Redis key validation**: `InputValidator.is_safe_identifier(value)`.

### 2. Pre-v0.4.0 pickle-encoded embeddings round-trip as `embedding=None`

If your deployment has pre-v0.4.0 data with pickle-encoded embeddings, run this one-off migration script before deploying v0.5.0:

```python
import pickle
from uams.utils.embedding_serde import serialize_embedding, deserialize_embedding

# Pseudocode — adapt to your storage layer:
# SELECT id, embedding_blob FROM memories WHERE embedding_blob LIKE '\\x80%';
for mid, blob in rows_with_pickle_embeddings:
    try:
        data = pickle.loads(blob)   # one-time only, on controlled data
        new_blob = serialize_embedding(data)
        # cursor.execute("UPDATE ... SET embedding = %s WHERE id = %s", (new_blob, mid))
    except Exception:
        log.warning("could not migrate embedding for %s", mid)
```

After the migration, v0.5.0's strict JSON-only reader will accept all rows safely.

## What did NOT change

- Public API surface for `UniversalMemorySystem` / `MemoryStore` — same shape as v0.4.0.
- Storage backends (InMemory / SQLite / PostgreSQL / Redis / Neo4j / ChromaDB) — no behaviour changes beyond the now-stricter embedding reader.
- Configuration schema — new constraints are stricter, not looser; valid v0.4.x configs continue to work.
- Async API — unchanged.

## Suite

488 tests / 0 errors / 3 pre-existing failures (unrelated: `test_large_chinese_text` perf threshold, `test_shutdown_persists_working` test-logic bug, `test_10k_sqlite_persistence` list_all clamp).

## Install

```bash
pip install "universal-agent-memory[all]==0.5.0"
```

## What's coming

A+ rating still requires: real production case study, real LLM monthly E2E report, third-party pen-test, 6-backend cluster failover drill, Helm chart. None of these were addressed in this release.

See `PRODUCTION_ASSESSMENT.md` v8 for the full picture.
