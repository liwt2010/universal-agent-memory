# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
