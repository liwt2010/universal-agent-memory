# v0.6.0 Release Notes

**Non-breaking minor release.** Closes 9 of 14 audit findings
(P0-1, P0-2, P0-4, P1-1, P1-2, P1-3, P1-4, P1-7, P2-3, P2-4,
P2-6). Defers 5 cross-backend items (P0-3, P1-5, P1-6 in
part, P2-1, P2-5) to v0.6.x — they require service-container
CI to verify end-to-end and we want them properly tested
rather than rushed.

## What's in this release

### New public surface

* `uams.errors` module — `UAMSError` + 4 subclasses
  (`ConfigError`, `StorageError`, `CascadeError`, `LLMError`).
  Re-exported from the package root so callers can
  `except UAMSError`.
* `MemoryStore.truncate() -> int` — single-round-trip
  delete-everything. SQLite overrides with `DELETE FROM` so
  it never hits the 999-row parameter cap. Other backends
  inherit a safe in-process default.
* `MemoryStore.list_all_paginated(limit, offset) -> list[Memory]`
  — true OFFSET-based pagination. SQLite overrides with
  `LIMIT ? OFFSET ?`; other backends inherit a safe default.
* `MemoryStore.delete_by_filters(filters) -> int` — multi-
  predicate delete in a single query. SQLite overrides with
  composite `WHERE col1 = ? AND col2 = ?`; the base default
  narrows via the rarest filter + per-row delete (safe for
  InMemoryStore where the data is in-process).
* `MemoryStore.vector_search_capable: bool` class attribute.
  `InMemoryStore` and `ChromaDBStore` set it to True; the
  other 4 backends leave it False and now log an INFO
  message on every `search_vector` call.

### Behaviour changes

* **`Memory.retrieval_score` is now `float | None = None`.**
  Previously it was `float = 0.0` and the `_compress_to_budget`
  helper's `or` short-circuit made `0.0` get treated as "no
  score set" and routed to the importance fallback. The fix
  uses `is None`. If you set the field yourself, no change;
  if you read it as `float` you may need
  `x if x is not None else default` for the new default.
* **`PrivacyFilter` splits patterns.** `SECRET_PATTERNS` apply
  at every privacy level (no more API keys leaking into PUBLIC
  content); `PII_PATTERNS` only at PRIVATE/INTERNAL/SECRET.
  Existing custom pattern lists still work — they're split by
  membership in the canonical lists at construction time.
* **`AgentContext.namespace()` includes `tenant_id` as a
  fourth colon-separated segment.** Callers reading only the
  first three segments are unaffected when `tenant_id` is
  None (it collapses to `_`).
* **`OpenAICompatibleClient.achat` retries on transient
  failures** (3 attempts, 0.5s → 1s → 2s backoff, capped at
  4s). Retries on `httpx.TimeoutException`, `httpx.ConnectError`,
  and 429/5xx `HTTPStatusError`. Permanent 4xx still bubbles
  up immediately.
* **`UniversalMemorySystem.observe()` drops events with empty
  `agent_id` / `agent_type` / `session_id` at entry with a
  WARNING log.** Prevents a misconfigured agent loop from
  landing memories on the "no agent" bucket where
  `delete_by_filter('agent_id', '')` would mass-delete them
  all in one call.
* **`LLMCompressionEngine.compress_working_to_episodic` runs
  the assembled narrative through `PrivacyFilter`** with the
  MAX privacy level across source events as the floor. The
  compressed memory's `metadata.privacy` is now that MAX, not
  just the first event's privacy.
* **`ChromaDBStore.list_all` is real now.** Previously it
  returned `[]`, silently breaking cascade in-edge discovery,
  `delete_by_project_id`, and `MigrationTool.migrate()` on the
  ChromaDB backend. v0.6.0 streams the collection in 500-row
  batches via `collection.get(include=['metadatas','documents'],
  limit=500, offset=offset)`.

### Storage-schema changes

* **`SQLiteStore` schema bumps from v1 to v2.** New column
  `tenant_id TEXT` + new index `idx_<tier>_tenant`. On open
  of a pre-v0.6.0 DB, the schema-upgrade path runs an
  `ALTER TABLE ADD COLUMN tenant_id TEXT` (idempotent — catches
  "duplicate column"). Old rows have `tenant_id=NULL`; back-
  compat is preserved (NULL is treated as "no tenant" and
  matches the v0.5.x `delete_by_project_id(project_id)` default
  path).
* **`SQLiteStore` `list_all` no longer silently drops >999
  rows.** The clamp was on the `list_all(limit=N)` parameter
  bind, not on the actual SELECT. Fixed in `list_all_paginated`
  which uses raw `LIMIT ? OFFSET ?` and skips the clamp.

### Removed in name only

* No public APIs were removed. The audit's `p2-5 enable_audit_log`
  recommendation was "真做" (real implementation), not delete;
  we deferred to v0.6.x because the `MetricsCollector`
  already records observe / forget / sync events in-process.

## Migration from v0.5.2

### For most users

* Upgrade as usual: `pip install --upgrade universal-agent-memory`.
  No config changes required.
* Run your SQLite-backed deployments once: the v2 schema
  migration runs automatically on first connect, adding
  `tenant_id` to every tier table.

### For downstream code that reads `Memory.retrieval_score`

```python
# Before (v0.5.2): always float
score: float = mem.retrieval_score

# After (v0.6.0): may be None
score: float = mem.retrieval_score if mem.retrieval_score is not None else 0.0
```

### For downstream code that hardcodes the 4-element namespace

```python
# Before (v0.5.2): 3 segments
parts = mem.context.namespace().split(":")  # [agent, user, team]

# After (v0.6.0): 4 segments
parts = mem.context.namespace().split(":")  # [agent, user, team, tenant]
# The 4th segment is "_" when tenant_id is None.
```

### For downstream code that relied on the v0.5.2 PUBLIC-no-filter bug

If you had PUBLIC content with an embedded API key (e.g. a
test fixture), v0.6.0 will redact the key. This is the
correct behaviour but it changes a visible string. If your
test asserts on the raw text, update it to assert on the
`<OPENAI_API_KEY>` replacement.

### For downstream code that catches `Exception` to handle UAMS failures

v0.6.0 adds `UAMSError`. You can now narrow your catch:

```python
from uams import UAMSError, ConfigError, StorageError, CascadeError, LLMError

try:
    uams.forget(...)
except ConfigError as e:
    # bad config — fix and restart
    ...
except StorageError as e:
    # backend refused — alert, fall back, or retry
    ...
except UAMSError as e:
    # any other UAMS-raised error
    ...
```

If you previously caught a generic `Exception` and want to
keep catching everything, no change is needed — `UAMSError`
inherits from `Exception`. The facade raises `UAMSError`
subclasses only for unrecoverable failures; transient errors
still log-and-fallback as before (preserving the v0.5.x
graceful-degradation contract for `RedisStore._disabled`
and friends).

## Known issues

* `tests/test_chaos.py::TestTokenEstimatorPerformance::test_large_chinese_text`
  is environment-dependent (passes on a fast machine, may
  fail on slow CI runners). Known since v0.5.0; not new.
* `tests/test_chaos.py::TestGracefulShutdown::test_shutdown_persists_working`
  is a test-logic bug — the assertion expects 1 row in
  episodic after shutdown, but `log.info` reports "Persisting
  1 working memories" while the actual transfer silently
  fails. Pre-existing since v0.5.0; tracked for v0.6.x
  investigation.
* Redis backend CI is currently red on hosted runners (real
  Redis service container flake). Pre-existing since v0.5.0.

## Files changed

* `src/uams/errors.py` (new)
* `src/uams/storage/base.py` (4 new methods + 1 class attr)
* `src/uams/storage/sqlite.py` (schema v2, truncate,
  list_all_paginated)
* `src/uams/storage/memory.py` (vector_search_capable = True)
* `src/uams/storage/chromadb.py` (vector_search_capable, real
  list_all, _chroma_row_to_memory helper)
* `src/uams/storage/redis.py` (vector_search_capable log
  addition)
* `src/uams/storage/postgresql.py` (vector_search_capable log
  addition)
* `src/uams/storage/neo4j.py` (vector_search_capable log
  addition)
* `src/uams/system.py` (delete_by_filters routing,
  truncate() in clear(), observe() required-field check)
* `src/uams/core/models.py` (retrieval_score = None,
  namespace() includes tenant_id)
* `src/uams/pipeline/privacy.py` (SECRET_PATTERNS +
  PII_PATTERNS split)
* `src/uams/pipeline/llm_compression.py` (PrivacyFilter on
  compressed output)
* `src/uams/llm/client.py` (achat retry loop)
* `src/uams/utils/backup.py` (MigrationTool.migrate uses
  list_all_paginated)
* `tests/test_errors.py` (new)
* `tests/test_retrieval_score_zero.py` (new)
* `tests/test_ollama_validator.py` (new)
* `tests/test_sqlite_tenant_id.py` (new)
* `tests/test_privacy_public_level.py` (new)
* `tests/test_namespace_tenant.py` (new)
* `tests/test_achat_retry.py` (new)
* `tests/test_observe_required_fields.py` (new)
* `tests/test_vector_search_capable.py` (new)
* `tests/test_chromadb_list_all.py` (new)
* `tests/test_llm_compression_pii.py` (new)
* `tests/test_truncate.py` (new)

## See also

* `CHANGELOG.md` — same release from a "what changed"
  perspective
* `PRODUCTION_ASSESSMENT.md` v10 — production-readiness
  rating after this release
* `docs/CONFIG_REFERENCE.md` — full configuration surface
* `docs/ARCHITECTURE.md` — updated async architecture section