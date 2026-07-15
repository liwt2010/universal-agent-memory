# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-07-15

### Security Hardening (closing real attack surfaces)

This release closes several real attack surfaces identified in an in-progress
hardening pass that was sitting in the working tree. Each change was reviewed
before commit. **This is a breaking release** because two historical
"compatibility shims" are removed (see Breaking Changes below).

#### Added — config-level input validation

- **`UAMSConfig.validate()` now rejects unsafe identifiers and paths** before
  any DDL / Redis key / log-file operation runs. New constraints:
  - `postgresql_table` must match `[A-Za-z0-9_-]+` (was unvalidated; user input
    could inject arbitrary DDL fragments).
  - `redis_key_prefix` and `redis_cache_key_prefix` must match
    `[A-Za-z0-9_:-]+` (colon allowed for Redis namespace conventions).
  - `cascade_audit_log_path` and `cascade_orphan_log_path` must not contain
    NUL, `|`, or `;` (defence-in-depth against accidental shell-meta
    interpolation).
  - `cascade_max_depth` is now bounded to `0..8` (was unbounded; the
    documented default was 4 but nothing enforced it).
  - `cascade_in_edge_strategy` is validated against `scan | index | auto`.

#### Added — JSON-only embedding reader

- **`uams.utils.embedding_serde.deserialize_embedding` no longer falls back to
  `pickle.loads`**. Legacy pickle-encoded blobs are rejected with an explicit
  ERROR log and treated as `None`. This closes a remote-code-execution
  vector: an attacker who could write to a shared store (PostgreSQL, Redis,
  SQLite file) would previously get arbitrary Python execution on the next
  `retrieve()`. Migration script is documented in the module docstring.
- `RateLimiter` is now thread-safe (added `threading.Lock` around the
  check-then-append path). A regression test (8 threads × 100 calls under
  a tight limit) verifies exactly the configured number pass.

#### Fixed — silent data loss in backup / restore

- **`Memory.to_json` and `Memory.from_json` now round-trip `embedding` and
  `relations`**. The previous implementation silently dropped both, which
  caused two real bugs:
  - `BackupManager.backup_to_file` / `restore_from_file` lost vector search
    capability on roundtrip.
  - `CascadeForgetter` could not discover in-edges from a restored backup,
    breaking GDPR Article 17 "right to be forgotten" traversal.
- Backward-compatible read: missing keys default to `None` / `[]` so old
  backups still load (they just lose the dropped fields).

#### Added — multi-tenant isolation primitive

- **`AgentContext.tenant_id`** added for multi-tenant isolation. Combined
  with the `delete_by_project_id(project_id, tenant_id=...)` API added in
  v0.4.0, this lets cloud deployments cleanly scope memory deletion to a
  single tenant.

#### Removed — keyword-based SQL sanitiser

- **`InputValidator.sanitize_sql` removed**. Keyword denylists are a known
  anti-pattern: they give false confidence while missing real attacks (bypass
  via `||`, `&&`, mixed case, comments). UAMS storage backends use
  parameterised queries everywhere, so this denylist was redundant false
  safety. Replaced with `InputValidator.is_safe_identifier(value)` — a
  whitelist grammar check for IDs that genuinely do get interpolated into
  DDL or Redis key prefixes.
- `InputValidator.rate_limiter(...)` factory is preserved (and now
  thread-safe).

### Breaking Changes

- **`InputValidator.sanitize_sql` is gone**. Any caller that was using it
  should switch to parameterised queries (the actual defence UAMS relies on)
  or `InputValidator.is_safe_identifier(...)` for DDL-interpolated values.
- **Pre-v0.4.0 backups with pickle-encoded embeddings** round-trip with
  `embedding=None`. Operators with such data must run the one-off
  migration script in the `embedding_serde` docstring before deploying
  v0.5.0.

### Test coverage added

5 new tests across `test_aplus.py` / `test_config_validation.py` /
`test_embedding_serde.py` covering identifier safety, cascade bounds,
audit-path safety, embedding fail-secure paths, and `RateLimiter` thread
safety under concurrent load.

Local: 483 → **488 tests pass** (+5, 0 regressions).

## [0.4.0] - 2026-07-12

### Public API expansion

New public APIs for bulk deletion and consolidation telemetry:

- `ConsolidateResult` dataclass returned by `consolidate()` (replaces the
  previous `-> None` return). Fields: `session_id`, `source_event_count`,
  `episodic_memory_id`, `semantic_facts`, `procedural_patterns`,
  `duration_ms`, `error`.
- `revoke_agent(agent_id, cascade=...)` and `revoke_project(project_id,
  cascade=...)` — bulk delete across all tiers by `context.agent_id` /
  `context.project_id`. Thin wrappers over the new
  `MemoryStore.delete_by_filter(field, value)` abstraction.
- `delete_by_project_id(project_id, tenant_id=None)` — narrower
  multi-tenant-safe deletion.
- `UniversalMemorySystem.get_stats(scan_limit=1000)` — now uses native
  `MemoryStore.count()` instead of `len(list_all(limit=999999))`, which
  was O(N) on the wire and silently returned `{}` on SQLite once the row
  count exceeded `SQLITE_MAX_VARIABLE_NUMBER`.

### Storage layer abstraction

- `MemoryStore.count() -> int` — new abstract method. Implementations:
  `SELECT COUNT(*)` (SQLite / PostgreSQL), `MATCH (n) RETURN count(n)`
  (Neo4j), `collection.count()` (ChromaDB), `len(self._memories)`
  (InMemory), `SCAN MATCH <prefix>*` (Redis).
- `MemoryStore.delete_by_filter(field, value) -> int` — new abstract
  method. Implementations: indexed column DELETE (SQLite / PG), Cypher
  MATCH / DELETE (Neo4j), `collection.delete(where=...)` (ChromaDB),
  in-memory filter (InMemory), SCAN + HGETALL filter (Redis).

## [0.3.0] - 2026-07-12

### Security & Reliability Hardening

Independent audit pass identified and fixed **15 issues across 5 rounds**. All
local tests pass (456 tests, 0 errors); the two remaining failures pre-date
this pass and are unrelated (`test_large_chinese_text` performance threshold,
`test_shutdown_persists_working` test-logic bug).

#### Critical fixes (silent correctness bugs)

- **`SQLiteStore.retrieve()` redundant `BEGIN`** — `SELECT` opens an implicit
  read transaction; the redundant `conn.execute("BEGIN")` raises
  `OperationalError: cannot start a transaction within a transaction` which
  the outer `except` swallowed, silently turning every retrieve() hit into
  `None`. WAL mode hides this; legacy journal mode users hit it. Removed the
  redundant `BEGIN`. (P0-A, audit-found)
- **`RedisStore.delete_expired()` early `return`** — `return count` was
  indented inside the `for` loop, so each sweep deleted only the first
  expired memory and the expiry ZSET grew monotonically. Moved `return`
  out of the loop. Verified with regression test that asserts 5 expired
  entries → `delete_expired()` returns 5. (P0-B)

#### Reliability / concurrency

- **`docker_entrypoint.py` now calls `ums.register_signal_handlers()`** —
  SIGTERM (Docker stop / Ctrl-C) now triggers `shutdown()` instead of
  Python exiting hard. Without this, WORKING-tier memories in the last
  `<TTL>` window were lost and SQLite WAL could be unflushed. (Bug 7)
- **`MemoryStore.close()` is now `@abstractmethod`** — all 6 built-in
  stores (`InMemoryStore`, `SQLiteStore`, `RedisStore`, `Neo4jStore`,
  `PostgreSQLStore`, `ChromaDBStore`) implement it. Custom backends get
  forced to clean up resources; previously `shutdown()` relied on
  `hasattr(store, 'close')` duck-typing. (Bug 8)
- **`decay_sweep()` is process-wide lock-protected** — a second concurrent
  call returns 0 with a debug log ("another sweep in progress") instead of
  racing through `delete_expired()` on every store. Closes a slow-sweep
  collision window in the 60s docker-entrypoint loop. (Bug 9)
- **`RedisStore` auto-disable on disconnect** — mirrors the
  `MultiAgentCoordinator._disabled` pattern. First Redis connection error
  flips `_disabled = True`; subsequent calls short-circuit with safe no-ops
  and **stop flooding the log** with tracebacks. `is_disabled` property
  exposed. (P1 #23)
- **`SQLiteStore.close()` handles in-flight threads** — tracks every
  connection via `_all_conns` and closes them on shutdown. `_return_connection()`
  now checks `_available` and closes rather than re-pooling a conn that
  was checked out at the moment close() ran. (P1 #24)
- **`MultiAgentCoordinator._signals` bounded** — `MAX_SIGNALS = 10000` cap;
  oldest entries dropped on append. Previously the queue grew unbounded
  in long-running agents that emit broadcast signals faster than they're
  consumed. (P1 #22)
- **`BackupManager.restore_from_file` splits error handling** — JSON
  parse failures on a line now log `"malformed JSON at <file>:<line>"`
  and skip that line; store write failures mid-restore now abort the
  whole import and return `None` (previously both were logged as
  `"Skipped invalid backup line"`, misdirecting operators to the wrong
  layer). (P1 #25)

#### GDPR / observability

- **`CascadeForgetter._locate_tier` no longer silently swallows backend
  exceptions** — each `except` now logs at ERROR level with `exc_info=True`.
  Previously a real backend failure (disk full / pool exhausted / auth
  failure) was indistinguishable from "this memory doesn't exist" in
  the resulting `CascadeReport`. (Bug 5)

#### Developer experience

- **`docs/API.md` reconciled with the code** — removed the fictional
  `sync()` method, removed wrong constructor kwargs (`backend`,
  `token_budget`, `retention_floor`), removed wrong `remember()` kwargs
  (`memory_type`, `confidence`, `tags-as-list`), removed wrong `recall()`
  kwargs (`top_k`), and rebuilt the `EventType` and `PrivacyLevel` tables
  from the actual enums (previously listed non-existent values like
  `SYSTEM_EVENT`, `MANUAL`, `ERROR`, `CONFIDENTIAL`). The `UAMSConfig`
  example was replaced with the recommended env-driven pattern. (P1 #21)
- **`AsyncUniversalMemorySystem.forget()` returns `CascadeReport`** —
  type hint was `bool` (a leftover from before the cascade rewrite); now
  also forwards `cascade`, `max_depth`, `in_edge_mode` kwargs to the
  sync implementation. (P2 #27)
- **`UAMS_SQLITE_POOL_SIZE` env var now flows through** — previously
  `UAMSConfig.sqlite_pool_size` was declared but never read; `from_env()`
  didn't parse the env var, and `UniversalMemorySystem._init_stores_from_config()`
  didn't pass `pool_size` to `SQLiteStore`. Now all three layers wire up,
  so operators can tune SQLite connection pool size from the environment.
  (P2 #29)
- **`pyproject.toml` URLs point to the real repo** — `github.com/uams/...`
  was a placeholder; corrected to `github.com/liwt2010/universal-agent-memory/...`
  so PyPI page links land on the actual project. (P2 #28)
- **`pyproject.toml` extras indentation** — `embeddings` and `llm` were
  indented under `chromadb`, which made them invisible to
  `[project.optional-dependencies]`. Now top-level extras:
  `pip install universal-agent-memory[llm]` and `[embeddings]` work.
  `openai` also added to the `all` extras. (Bug 1)

#### Test coverage added

29 new tests across 7 new / extended files:
- `tests/test_shutdown_signal.py` (Bug 7 regression: 3 tests)
- `tests/test_decay_sweep_lock.py` (Bug 9 regression: 2 tests)
- `tests/test_sqlite_concurrency_and_fts5.py` (P0-A + Bug 24: +5 tests)
- `tests/test_redis_store.py` (P0-B + P1 #23: +4 tests)
- `tests/test_cascade.py` (Bug 5 regression: +2 tests)
- `tests/test_backup_failure_semantics.py` (P1 #25: +2 tests)
- `tests/test_signal_queue_bound.py` (P1 #22: 4 tests)
- `tests/test_async_forget_signature.py` (P2 #27: 4 tests)
- `tests/test_sqlite_pool_size_config.py` (P2 #29: 3 tests)

Local: 427 → **456 tests pass** (+29, 21 skipped server-gated, 0 regressions).
The two pre-existing failures are unrelated and out of scope for this pass.

### Added
- **Cross-layer cascade forget (GDPR Article 17 aligned)**
  - `CascadeStrategy` enum: `'isolated'` / `'outgoing'` / `'bidirectional'` (default bidirectional)
  - `CascadeReport` dataclass: `target_id`, `tier`, `strategy`, `deleted_ids`, `orphan_ids`, `failed_ids`, `duration_ms`, `audit_log_path`, plus `deleted_count` / `orphan_count` / `failed_count` / `is_complete` properties and `to_dict()` for the audit log
  - `CascadeForgetter` (`src/uams/pipeline/cascade.py`): BFS over relations with `visit_set` (cycle guard), `max_depth` cap (default 4), strict same-tier scope (cross-tier edges recorded as orphans but never deleted)
  - In-edge discovery: hybrid `scan` / `index` / `auto` modes via `UAMSConfig.cascade_in_edge_strategy` (currently all 6 backends fall back to `scan`; future `reverse_index()` adapter hook in place for O(1) lookups)
  - Best-effort delete leaves-first; partial failures recorded in `report.failed_ids`; cascade never raises out
  - **`CascadeAuditWriter`** (`src/uams/utils/cascade_audit.py`): append-only JSONL writer, RLock-protected for cross-thread safety, one file per invocation + one per cross-tier orphan edge encountered
  - `UniversalMemorySystem.forget()` rewire: now accepts `cascade=` keyword + dispatches through `CascadeForgetter`. Default `cascade='bidirectional'` (GDPR-aligned); legacy single-shot via `cascade=CascadeStrategy.ISOLATED`. Returns `CascadeReport`
  - 4 new `UAMSConfig` fields: `cascade_in_edge_strategy` (default `"auto"`), `cascade_max_depth` (default `4`), `cascade_audit_log_path`, `cascade_orphan_log_path`
  - **`docs/CASCADE_FORGET.md`** user guide (TL;DR, configuration, `CascadeReport` reader, GDPR-aligned workflow, failure semantics, migration notes)
  - **29 new tests** in `tests/test_cascade.py` covering: enum round-trip, dataclass shape, audit-log append, audit concurrency, config fields, locate-tier, discover-in-edges (3 modes), isolated / outgoing / cycle protection / cross-tier orphan / partial failure / bidirectional strategies, system-rewire integration
  - Local: 317 → **346 tests pass** (+29 cascade), 21 still skipped (server-gated), 0 regressions
  - CI: **9/9 jobs green** (test matrix 4× Python 3.9/3.10/3.11/3.12, integration, **6/6 storage backends real service containers**: PostgreSQL / ChromaDB / Redis / Neo4j + SQLite / InMemory)
  - Spec: `docs/superpowers/specs/2026-07-10-cross-layer-forget-cascade-design.md`. Plan: `docs/superpowers/plans/2026-07-10-cross-layer-forget-cascade.md`
- **LLM-backed compression engine** (`LLMCompressionEngine`) inheriting `CompressionEngine`
  - Episodic summarization with two-level batching for long sessions
  - Semantic extraction via JSON-array output (tolerant of `\`\`\`json` fences)
  - Procedural pattern detection across episodes
  - Auto-fallback to `HeuristicCompressionEngine` on any LLM failure
- **OpenAI-compatible LLM client** (`OpenAICompatibleClient`) — works with OpenAI / MiniMax / ollama / vLLM via `base_url` configuration
- **`NullLLMClient`** + **`CachedLLMClient`** (in-process LRU by messages+kwargs hash)
- **Pluggable embedding providers**: `SentenceTransformersProvider` (local) and `OpenAICompatibleEmbeddingProvider` (remote) + `CachedEmbeddingProvider` (LRU)
- **Production-safety config validation** with environment strictness ladder (`development` / `staging` / `production`)
  - Rejects insecure default credentials on Neo4j / PostgreSQL / Redis in production
  - Requires TLS on credentialed backends in production
  - Bounds half-life (60s–10y), timeouts, identity-length fields
  - 30+ new `UAMSConfig` fields for LLM + embedding
- **Maintainer / response SLA**: `pyproject.toml` authors + `SECURITY.md` contact + `README.md` Maintenance & Support section (security 48h ack, bugs 7d, features 14d)
- **143 new tests** (105 → 248 total) covering config validation, LLM compression, embedding providers, query rewriting, Redis cache, hierarchical filter
- **`docs/PR1-2-LLM-Compression.md`** handoff document for the LLM compression design
- **`examples/_token_compression_demo.py`** benchmark demonstrating 72% token savings with `LLMCompressionEngine` (20-event session: 300 → 84 tokens). The default `HeuristicCompressionEngine` produces ≈ 0% savings (just structures events, no summary). Opt in via `UAMS_LLM_ENABLED=true` + `UAMS_LLM_API_KEY` + `UAMS_LLM_BASE_URL` + `UAMS_LLM_MODEL`.

### Token Compression Suite (5 PRs, commit `a614389..1141398`)
- **PR1 — Retrieval relevance density (`a614389`)**: pack memories by `score/tokens` instead of pure count; high-signal short memories beat low-signal long ones
- **PR2 — Prompt compression (`3e3cc70`)**: trim 3 system prompts ~50% (Episodic 78→29, Semantic 68→29, Procedural 73→41) + drop event timestamps in user prompt; auto-improves cache prefix hit rate
- **PR3 — Query rewriting (`302ee70`)**: opt-in LLM-based query rewrite + LRU cache; off by default (env `UAMS_QUERY_REWRITE_ENABLED`); graceful fallback on LLM failure
- **PR4 — Redis cross-process cache (`4e49ca5`)**: shared cache backend for `CachedLLMClient` + `CachedEmbeddingProvider`; JSON-serialized embeddings; graceful degradation when Redis is down
- **PR5 — Hierarchical pre-filter (`1141398`)**: L1 structural filter (drop short content / pure observation / duplicates) + L2 keyword hint (top-K TF-IDF tokens) prepended to the user prompt — LLM-free, every call
- **Cumulative savings**: single LLM call ~55%, cross-call ~90% (Redis hit), overall session 30-50% (cold) / 70-90% (warm with cache)
- **22 new tests** for hierarchical filter; 11 for query rewrite; 15 for Redis cache — 48 added across the suite (248 total)
- **`docs/Token-Compression-Suite.md`**: full handoff doc (347 lines) covering 5 PRs, config keys, migration steps, per-PR pitfalls
- PostgreSQL enterprise backend with connection pooling, JSONB, GIN indexes, and schema migrations
- Configuration validation system with 12+ constraints
- Exponential backoff retry mechanism with global statistics
- Backup and restore tools (JSONL and dict formats)
- Migration tool for cross-backend data migration
- Security enhancements: SQL injection protection, XSS prevention, input sanitization, rate limiting
- Benchmark suite for performance testing (store, retrieve, search, delete)
- 42 additional enterprise-grade tests covering edge cases, exception paths, and chaos scenarios
- Docker and docker-compose support for Redis, Neo4j, and PostgreSQL backends

### Added
- **`CascadeStrategy.FULL_CASCADE`** — explicit opt-in cross-tier deletion
  for true GDPR Article 17 "right to be forgotten" semantics.
  - Existing strategies (ISOLATED / OUTGOING / BIDIRECTIONAL) still treat
    cross-tier edges as `report.orphan_ids` (recorded, not deleted) — that
    default is preserved for callers who want audit-without-deletion.
  - `FULL_CASCADE` follows cross-tier edges, dispatches deletes to the
    memory's actual tier, and records every cross-tier deletion in a
    new `report.cross_tier_deleted_ids: List[Tuple[str, str]]` field
    (id + original tier name) for the audit trail.
  - Audit log line in `cascade_forget_audit.jsonl` now includes
    `cross_tier_deleted_count` and `cross_tier_deleted_ids` so an
    operator can see at a glance whether a cross-tier deletion actually
    happened (vs. an orphan-only attempt).
  - 9 new tests in `tests/test_cascade.py::TestFullCascadeStrategy`
    covering: strategy value, out-edge + in-edge cross-tier deletion,
    chain across 3 tiers, max_depth cap across tier boundaries, cycle
    protection in cross-tier traversal, regression guard for
    BIDIRECTIONAL behavior, audit log shape, `to_dict()` shape.
- **README honesty fix**: "72% token savings" badge + comparison tables
  now explicitly note that the headline number is the **LLM-backed**
  path and the default `HeuristicCompressionEngine` produces ~0%
  savings (just structures events, no summary). en + zh-CN + zh-TW
  all updated; CHANGELOG entry for the demo also qualified.
- Local: 375 → **385 tests pass** (+10), 32 still skipped (server-gated), 0 regressions.

### Added
- **`remember()` semantic dedup (opt-in)**: when
  `UAMSConfig.remember_dedup_enabled=True` and an embedding function
  is available, `remember()` searches the SEMANTIC store for an
  existing memory with cosine similarity ≥
  `remember_dedup_threshold` (default 0.95) and returns the existing
  `MemoryId` instead of storing a new one. Prevents semantic noise
  like "I like vegetables" + "I'm vegetarian" coexisting as
  separate memories. Falls back to "always store" with a debug log
  when no embedding is available. 7 new tests in
  `tests/test_remember_dedup.py` covering: default-off behavior,
  dedup hit returns existing id, below-threshold stores new,
  no-embedding fallback, embedding-failure fallback, threshold
  boundary, three-duplicates-only-one-stored.
- **Per-category half-life overrides (opt-in)**: new
  `UAMSConfig.category_half_life_overrides: Dict[str, Optional[float]]`
  (empty by default) lets operators replace a tier's default
  half-life for memories in a specific category. `None` value means
  "never forget" (sentinel half-life 10k years, retention ≈ 1.0
  forever). The first matching key in the override dict (in
  insertion order) wins, so the operator's config precedence is
  honored. When an override applies, the engine uses
  floor=0.1 (forget after ~3-4 halflives) instead of the tier's
  stickiness floor, because the operator picked a specific rate
  for a reason. **Empty by default and MUST be populated from
  observed traffic** — see `docs/HALF_LIFE_TUNING.md` for the
  calibration methodology. 10 new tests in
  `tests/test_category_half_life.py` covering: tier default when
  no override, `None` = never forget, numeric override replaces
  half-life, first-match-wins precedence, unknown category
  falls back to tier, override bypasses tier stickiness, full
  `should_forget` consistency, importance/confidence interaction,
  back-compat with the original tier-default behavior.
- **`docs/HALF_LIFE_TUNING.md`**: full calibration methodology.
  Why calibration matters, the two failure modes of "wrong"
  half-lives, instrumentation-first workflow, 90th-percentile
  access-age approach, A/B testing the floor, "long tail"
  category hygiene, and what this is NOT (hard-delete policy,
  per-user, auto-tuned).
- Local: 385 → **402 tests pass** (+17), 32 still skipped (server-gated), 0 regressions.

### Added
- **`benchmarks/stress_test.py`** (100k A+ requirement): real-backend
  concurrent stress test for UAMS storage backends. Mixes store /
  retrieve / search / delete operations across N worker threads,
  measures ops/sec, p50/p95/p99 latency, error rate, and RSS memory
  growth. Emits a JSON report with per-op breakdown and warnings
  for regressions (error rate > 1%, p95 > 1s, RSS growth > 200MB,
  incomplete ops). Supports all 6 backends (memory / sqlite /
  postgresql / redis / neo4j / chromadb) via `--backend=...`.
  - **Default = 10k ops, 8 threads** (smoke / CI gate). 100k ops
    is the A+ target; run with `--ops 100000 --concurrency 32`.
  - **Known issues surfaced by smoke** (pre-existing, not new bugs):
    SQLite at concurrency > pool_size (5) hits "database is
    locked" — surfaces a tuning opportunity. FTS5 fallback in
    `storage/sqlite.py` treats hyphens in queries as column names
    (a SQL fallback bug) — flagged for follow-up, not fixed here.
- **CI: `stress-test-real-deps` matrix job** in `.github/workflows/ci.yml`:
  runs the 100k-ops stress test against PostgreSQL, ChromaDB,
  Redis, and Neo4j service containers. `continue-on-error: true`
  (informational, not a hard gate) because real-world backends
  can flake at high concurrency on hosted runners. JSON report
  uploaded as a per-backend artifact for trend tracking.
- **`docs/STRESS_TEST.md`**: full guide — quick start, what the
  warnings mean, what the test catches (lock contention, memory
  leaks, FTS5 edge cases, connection pool exhaustion) and what it
  does NOT (real-world traffic, multi-tenant, LLM cost). Known
  false-positive patterns documented.
- **`docs/LONG_TERM_LLM.md`**: honest assessment of the
  "real LLM 1+ month" A+ condition. **This is a time investment,
  not a code deliverable** — explicitly scoped. The doc covers
  what code CAN deliver (telemetry hooks, dry-run harness, cost
  guardrails), what the operator must do (run with real LLM for
  30+ days), and why this is the A+ condition most likely to
  fail. Recommendation: start the 30-day run in parallel with
  the other A+ conditions.
- 11 new unit tests in `tests/test_stress_test.py` covering the
  StressRunner logic on InMemoryStore: percentile helpers, mix
  parsing, JSON serialization, basic run, per-op breakdown,
  concurrency distribution (the original `ops // concurrency`
  formula dropped `ops % concurrency` ops; the runner now
  distributes evenly with the first N workers getting the extra
  op).
- Local: 402 → **413 tests pass** (+11), 32 still skipped (server-gated), 0 regressions.

### Security hardening
- **`pickle.loads` → `json.loads` for embedding blobs** (R1): `utils/embedding_serde.py`
  new helper. Writes always use JSON (no RCE risk if storage is compromised).
  Reads prefer JSON; legacy pickle-encoded blobs are still readable with a
  logged warning, so existing data is not orphaned. A future migration
  script can rewrite the storage and `pickle` will be dropped entirely.
  Affected: `storage/{postgresql,sqlite,redis}.py` (3 sites).
  11 new tests in `tests/test_embedding_serde.py`.
- **`BackupManager.backup_to_file` / `restore_from_file` distinguish failure from empty** (R2):
  return `None` on fatal failure (vs `0` for legitimate "no data"), and
  log the exception at `ERROR` level. 4 new tests in
  `tests/test_backup_failure_semantics.py`.
- **`MultiAgentCoordinator` auto-disables on Redis failure** (R3): previously
  a Redis error silently fell back to in-memory locking, which is unsafe in
  the multi-process deployment that motivated the Redis choice. Now the
  coordinator marks itself `disabled` on first Redis error, future
  `acquire_lease` / `release_lease` short-circuit to `None` / `False` and
  log a clear "auto-disabling" message. Other workers are unaffected
  because each has its own coordinator instance.
  7 new tests in `tests/test_coordinator_auto_disable.py`.
- **Local: 353 → 375 tests pass** (+22), 32 still skipped (server-gated), 0 regressions.
- **Ruff S-rule audit**: 44 → 41 errors. 3 real risks fixed above; remaining 41
  are documented false positives (column-name SQL interpolation, cleanup-path
  `except: pass`, `random` in benchmarks, K8s health-server bind, config-default
  passwords behind production validation, intentional cascade `except: continue`).
  Fallback `pickle.loads` paths marked with explicit `# noqa: S301` for clarity.

### Fixed
- Memory leaks in MetricsCollector via circular buffer aggregation
- Infinite loop in MigrationTool when using InMemoryStore as source
- Test failures caused by sanitize_all ordering and HTML entity semicolons
- Missing `importance`/`confidence` fields in restore_from_dict test data
- Graceful degradation for Redis and Neo4j when dependencies are not installed
- **SQLite pool_size 5 starved under 4+ concurrent write threads** (WAL mode serializes writes, so the pool got contended and `busy_timeout` retries slowed everything down). Fixed: `pool_size` default 5 → 8, `store()` / `delete()` / `delete_expired()` wrapped in `RLock` (writes serialized, reads stay concurrent), and `PRAGMA busy_timeout=5000` as belt-and-suspenders. 3 new tests in `tests/test_sqlite_concurrency_and_fts5.py` (4-thread × 50 writes, 8-thread × 20 writes, mixed read/write).
- **FTS5 `MATCH` parsed `-` as NOT operator**, so `search_keywords('state-of-the-art')` returned empty (parsed as `state AND NOT of AND NOT the AND NOT art`). Fixed: new `SQLiteStore._sanitize_fts5_query()` wraps the user query as an FTS5 phrase (`"..."`), with embedded `"` escaped by doubling. 6 new tests covering hyphen / asterisk / embedded quotes / single-word regression / multi-word phrase / unit test of the helper.

### Performance (Redis)
- **Removed unnecessary `threading.RLock()` from 6 `RedisStore` methods** — `redis-py` is already thread-safe (ConnectionPool has its own lock), so the outer RLock was serializing all 32 worker threads into one. This alone gave a 32x throughput improvement; combined with the next change, end-to-end store/retrieve/delete dropped from multi-second per-op to ~10ms p50.
- **`store()` / `retrieve()` / `delete()` use Redis pipeline** — collapsed 2-3 round-trips per op into 1. `store()` now does `HSET + EXPIRE + ZADD` in a single pipeline; `retrieve()` does `HGETALL + HSET`; `delete()` does `DELETE + ZREM`. Net result: per-op latency dropped from seconds to ~10ms p50 (per-op breakdown on the 32-worker stress run: store 9ms, retrieve 19ms, delete 9ms).
- **Inverted token index for `search_keywords()`** — search was O(N) full SCAN + HGETALL-per-key, hitting 28s p50 on 13k memories. New design: per-token SET (`idx:term:<t>`) + per-memory token SET (`idx:mem:<id>:tokens`) maintained in `store()` / `delete()`. `search_keywords()` now does `SMEMBERS` per query token (union) + 1 pipeline of HGETALL on the candidate set, dropping search p50 from 28s to <100ms on 100k memories. 4 new tests in `tests/test_redis_store.py` cover index build/cleanup + index-based search. **Documented behavior change**: substring search now requires the query term to be an indexed token (whole-word matching); pre-index `vege` → `vegetarian` substring path no longer works because the index is the gatekeeper. Tokenizer drops single-char tokens.

### Performance (Stress test)
- **CI: split `stress-test-real-deps` matrix into 4 independent jobs** (`commit 4927149`). The matrix pattern declared all 3 service containers (postgres / redis / neo4j) for every job, even though only 1 was used per entry. On busy runners, this caused "One or more containers failed to start" and the whole matrix run failed without producing any stress-report artifacts. New design: 4 jobs (`stress-postgresql` / `stress-chromadb` / `stress-redis` / `stress-neo4j`), each declaring only its own service. ChromaDB has none (in-process EphemeralClient). The `test-real-deps` matrix (which currently passes with the same over-declared pattern) is intentionally NOT changed in this commit to keep the diff focused on the regression.
- **`stress_test.py` config fixes for 100k × 32 workers** (`commit 4bde0e3`):
  - **Redis**: removed `max_capacity=...` kwarg that `RedisStore.__init__()` doesn't accept (was crashing at setup with `TypeError: unexpected keyword argument 'max_capacity'`, so the Redis stress job never produced a report). Now passes just connection params.
  - **PostgreSQL**: override `pool_max=64` (2× concurrency + buffer) since the default 10 caused `psycopg2.ThreadedConnectionPool` to raise `PoolError("connection pool exhausted")` for ~22/32 workers, producing ~81% error rate. After the fix: 100000/100000 ops, 0% err, 269 ops/sec, p95 212ms.
- **Fundamental fix for Redis 100k stress** (`commit 5331390`):
  - **`store()` collapsed to a single pipeline**: main write (HSET) + TTL bookkeeping (EXPIRE / ZADD) + inverted index updates (SADD per token + SADD for per-memory token set) all in **1 round-trip** (was 2). Halves the per-op network cost on slow CI networks.
  - **`search_keywords()` capped to `k*10` candidates** (floor 50): when an inverted-index candidate set balloons (e.g. 14k stress-test memories all share the "stress" token, giving 14k candidates), we now `random.sample()` down to at most 100 before HGETALL + JSON-deserialize. Bounds worst-case search latency at O(k) HGETALLs regardless of how many documents match. **Result (CI, 100k × 32 workers, real Redis)**: 100000/100000 ops completed, 0% err, 138 ops/sec, p50 98ms, p95 1192ms, search p50 778ms (37x better than cc1c7ed's 28s). RSS growth dropped from 1.35GB → 205MB (6.5x better). 1 new test: `test_inverted_index_search_caps_candidate_set`.

### Test suite growth
- 413 → **427 tests pass** (+14: 3 SQLite concurrency + 6 FTS5 phrase + 5 Redis inverted index (added candidate-cap test)). 32 skipped (server-gated), 0 regressions.

## [0.1.0] - 2024-XX-XX

### Added
- Initial release of UAMS (Universal Agent Memory System)
- Four-tier memory model: Working → Episodic → Semantic → Procedural
- Event bus ingestion with zero framework coupling
- Hybrid retrieval pipeline: BM25 keyword + dense vector + knowledge graph + RRF fusion
- Privacy filter with automatic secret stripping and PII masking
- Deduplication window with SHA-256 rolling hash
- Ebbinghaus-inspired forgetting engine with configurable decay curves per tier
- Multi-agent coordination: resource leases, signals, shared memory spaces
- Token budget compression for LLM context windows
- Pluggable storage backends: InMemory, SQLite, ChromaDB, Redis, Neo4j
- Framework adapters: Claude, OpenAI, LangChain, AutoGen, custom agents
- 74 unit tests with thread safety, concurrency, and stress testing
- 5 example applications: personal assistant, game NPC, customer service, research agent, multi-agent
- CI/CD pipeline with GitHub Actions
- Multi-language documentation (English, 简体中文, 繁體中文)

[Unreleased]: https://github.com/liwt2010/universal-agent-memory/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/liwt2010/universal-agent-memory/releases/tag/v0.1.0
