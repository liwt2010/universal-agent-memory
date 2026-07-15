# UAMSConfig — Wired vs Unwired Fields

`UAMSConfig` is a frozen dataclass with 50+ fields spanning storage
backends, LLM tuning, embedding providers, retention, security, and
audit. About **half of those fields are not actually read by any
production code path** — the audit pass that landed v0.4.0 / v0.5.0
fixed the highest-impact ones (e.g. `sqlite_pool_size`, `cascade_*`)
but the rest were left as inert dataclass slots because removing them
would be a breaking change for deployment scripts that already set them.

This file is the source of truth for **which config keys actually
take effect** so operators don't waste time tuning keys that are
silently ignored.

## Tier 1 — Read and take effect

These keys feed directly into runtime behavior. Changing them in
production changes behavior.

| Key | Effect | Where it's read |
|---|---|---|
| `storage_backend` | Selects InMemory / SQLite / ChromaDB / Redis / Neo4j / PostgreSQL | `system.py:_init_stores_from_config` |
| `sqlite_path`, `sqlite_pool_size` | SQLite DB path + connection pool size | `system.py:_init_stores_from_config` |
| `working_ttl_seconds` | WORKING tier TTL (seconds) | `system.py:observe` |
| `episodic_half_life_seconds`, `semantic_half_life_seconds`, `procedural_half_life_seconds` | Ebbinghaus decay half-lives | `pipeline/forgetting.py` |
| `category_half_life_overrides` | Per-category half-life overrides | `pipeline/forgetting.py` |
| `default_token_budget` | Default LLM context token budget | `system.py:recall` |
| `max_raw_length` | Max chars per raw payload (truncate beyond) | `system.py:_truncate_raw` |
| `memory_capacity` | InMemoryStore LRU cap | `storage/memory.py` |
| `rrf_k` | Reciprocal Rank Fusion constant | `pipeline/retrieval.py` |
| `dedup_window_seconds` | SHA-256 dedup window | `pipeline/privacy.py` |
| `remember_dedup_enabled`, `remember_dedup_threshold` | Semantic dedup on `remember()` | `system.py:remember` |
| `cascade_max_depth`, `cascade_in_edge_strategy` | Cascade deletion depth + in-edge scan strategy | `pipeline/cascade.py` |
| `cascade_audit_log_path`, `cascade_orphan_log_path` | JSONL audit file paths | `pipeline/cascade.py` |
| `histogram_max_entries` | MetricsCollector ring-buffer cap | `health.py:MetricsCollector` (wired v0.5.1) |
| `event_bus_max_buffer` | EventBus subscriber queue cap | `bus/event_bus.py` |
| `default_privacy_level` | Default privacy for new memories | `core/models.py:AgentEvent` |
| `privacy_patterns` | Override default secret-detection patterns | `pipeline/privacy.py` |
| `strictness` | production / staging / development env ladder | `config.py:validate` |
| `environment` | top-level environment label | `config.py:validate` |
| `structured_logging` | JSON vs text logs | `config.py:from_env → configure_logging` |

## Tier 2 — Wired but with a caveat

| Key | Status |
|---|---|
| `llm_enabled`, `llm_base_url`, `llm_api_key`, `llm_model`, `llm_max_retries`, `llm_timeout_seconds`, `llm_cache_enabled`, `llm_cache_max_entries`, `llm_compression_max_events`, `llm_compression_target_ratio` | Read by `system.py:_build_compression_engine`. The fallback path uses HeuristicCompressionEngine when these fail to construct; only effective if an LLM client is actually wired up (see `llm/client.py`). |
| `embedding_provider`, `embedding_model`, `embedding_dim`, `embedding_cache_max_entries`, `embedding_timeout_seconds` | Read by `system.py:_build_embedding_fn` / `_build_embedding_provider`. Effective only when an embedding provider is configured; InMemoryStore and SQLite operate fine without one. |
| `redis_host`, `redis_port`, `redis_db`, `redis_password`, `redis_key_prefix`, `redis_enable_pubsub`, `redis_pool_max_connections` | Read by `storage/redis.py` constructor (if `storage_backend == "redis"`). |
| `neo4j_uri`, `neo4j_user`, `neo4j_password`, `neo4j_database` | Read by `storage/neo4j.py`. |
| `postgresql_host`, `postgresql_port`, `postgresql_database`, `postgresql_user`, `postgresql_password`, `postgresql_pool_min`, `postgresql_pool_max`, `postgresql_table` | Read by `storage/postgresql.py`. |
| `postgresql_use_tls`, `neo4j_use_tls`, `redis_use_tls` | **Stored but not applied to the driver connection**. They are validated by `UAMSConfig.validate()` only. The underlying redis-py / neo4j / psycopg2 drivers do not read these values. Operators wanting TLS must configure it via driver-specific env vars (e.g. `REDIS_URL=rediss://...`). |
| `connection_timeout_seconds`, `read_timeout_seconds` | Stored but only `read_timeout_seconds` is applied (in `llm/client.py`). Drivers use their own defaults for connect timeout. |

## Tier 3 — Declared but not currently read anywhere

These keys are in `UAMSConfig` and parsed by `from_env()`, but no code
in `src/uams/` reads them. **Operators setting these env vars see no
effect.** They are kept in the dataclass to preserve backward
compatibility with deployment scripts. A future release may wire them
up.

| Key | Intended (but unwired) purpose |
|---|---|
| `max_session_events` | Intended as a cap on `_session_events` per session; no code reads it. |
| `privacy_redaction_enabled` | Intended as a switch on `PrivacyFilter`; no code reads it (privacy filter is always on). |
| `enable_audit_log`, `audit_log_path` | Intended as a switch for an audit-log writer that was never implemented. Cascade-level auditing uses the separate `cascade_audit_log_path` key. |
| `enable_metrics` | Intended as a kill-switch for `MetricsCollector`; no code reads it (metrics always enabled when `HealthServer` is constructed). |
| `max_agent_id_length`, `max_user_id_length` | Intended as bounds enforced by `UAMSConfig.validate()`; declared but never validated. |
| `max_results_per_session` | Intended as a search cap; no code reads it. |
| `llm_max_tokens`, `llm_temperature` | LLM client chat defaults; `LLMClient.chat(...)` accepts these as kwargs and uses its own internal defaults, but the global config values are not pushed into the chat call by default. Operators can pass them through `OpenAICompatibleClient(temperature=...)`. |

## Tier 4 — Field removed / renamed in v0.5.x

| Old key | Status |
|---|---|
| `sanitize_sql` (was a method on `InputValidator`) | **Removed in v0.5.0** — keyword denylist was an anti-pattern. Use `is_safe_identifier` or parameterised queries. |

---

## Adding a new config key

If you add a key to `UAMSConfig`:

1. Add the dataclass field with a default and inline comment.
2. Add an entry in `from_env()` if the key should be overridable from the
   environment. Use `cls._env_int / _env_float / _env_bool / _env_str`.
3. Add validation in `validate()` if bounds or enum membership apply.
4. **Actually read it from somewhere in `src/uams/`** — otherwise it
   joins Tier 3 and contributes to operator confusion. If the wiring
   is blocked on missing downstream code, write a TODO comment
   pointing to the consumer that needs to be built.
5. Add the key to **Tier 1 of this document** with the consuming
   function name.

This document is the API contract for UAMSConfig. Operators reading
docs/API.md or this file are entitled to know which env vars are
load-bearing and which are aspirational.
