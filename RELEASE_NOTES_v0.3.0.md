# v0.3.0 — Security Audit Hardening

This release fixes **15 issues across 5 audit rounds** (P0/P1/P2). Suite: 427 → **456 tests** (+29, 0 regressions). Rating: B+/A- → **A-** per `PRODUCTION_ASSESSMENT.md` v7.

## P0 — silent correctness bugs

- **`SQLiteStore.retrieve()` redundant `BEGIN`** — `SELECT` opens an implicit read transaction; the redundant `conn.execute("BEGIN")` raised `OperationalError: cannot start a transaction within a transaction` which the outer `except` swallowed. Every retrieve() hit returned `None` in legacy journal mode (WAL hid it). Removed the redundant `BEGIN`.
- **`RedisStore.delete_expired()` early `return`** — `return count` was indented inside the `for` loop, so each sweep deleted only the first expired memory and the expiry ZSET grew monotonically. Moved `return` out of the loop. Regression test asserts 5 expired entries → `delete_expired()` returns 5.

## P1 — reliability / concurrency

- **`docker_entrypoint.py` SIGTERM handler** — `docker stop` now triggers `shutdown()` instead of Python exiting hard. WORKING-tier memories in the last `<TTL>` window and SQLite WAL are now flushed cleanly.
- **`MemoryStore.close()` is `@abstractmethod`** — all 6 built-in stores implement it. Custom backends get forced to clean up resources.
- **`decay_sweep()` process-wide Lock** — second concurrent call returns 0 with a debug log ("another sweep in progress"). Closes the slow-sweep collision window.
- **`RedisStore` auto-disable on disconnect** — mirrors the `MultiAgentCoordinator._disabled` pattern. First Redis connection error flips `_disabled = True`; subsequent calls short-circuit with safe no-ops and stop flooding the log.
- **`SQLiteStore.close()` handles in-flight threads** — tracks every connection via `_all_conns`; `_return_connection()` checks `_available` and closes rather than re-pooling a conn that was checked out at the moment close() ran.
- **`MultiAgentCoordinator._signals` bounded** — `MAX_SIGNALS = 10000` cap; oldest entries dropped on append.
- **`BackupManager.restore_from_file` splits error handling** — JSON parse failures skip that line; store write failures mid-restore abort the whole import and return `None`.

## GDPR / observability

- **`CascadeForgetter._locate_tier` logs backend exceptions at ERROR** — previously a real backend failure (disk full / pool exhausted / auth) was indistinguishable from "this memory doesn't exist" in `CascadeReport`.

## Developer experience

- **`docs/API.md` reconciled with code** — removed fictional `sync()`, wrong constructor kwargs (`backend`, `token_budget`, `retention_floor`), wrong `remember()` kwargs (`memory_type`, `confidence`, `tags-as-list`), wrong `recall()` kwargs (`top_k`), and rebuilt `EventType` and `PrivacyLevel` tables from the actual enums (previously listed non-existent values like `SYSTEM_EVENT`, `MANUAL`, `ERROR`, `CONFIDENTIAL`). `UAMSConfig` example replaced with the recommended env-driven pattern.
- **`AsyncUniversalMemorySystem.forget()` returns `CascadeReport`** — type hint was `bool` (leftover from before the cascade rewrite); now also forwards `cascade`, `max_depth`, `in_edge_mode` kwargs.
- **`UAMS_SQLITE_POOL_SIZE` env var wired** — previously declared but never read; `from_env()` didn't parse the env var and `UniversalMemorySystem._init_stores_from_config()` didn't pass `pool_size` to `SQLiteStore`. All three layers wire up.
- **`pyproject.toml` URLs point to the real repo** — `github.com/uams/...` was a placeholder; corrected to `github.com/liwt2010/universal-agent-memory/...`.
- **`pyproject.toml` extras indentation** — `embeddings` and `llm` were indented under `chromadb`; now top-level extras: `pip install universal-agent-memory[llm]` and `[embeddings]` work. `openai` also added to the `all` extras.

## Test coverage added

29 new tests across 7 new / extended files. Full list in `CHANGELOG.md`.

## What did NOT change

- Public API is **backward-compatible** with 0.1.0 (and the unreleased 0.2 work).
- No new dependencies.
- No breaking changes to existing user code.

## Still not A+

The A+ rating still requires: real production case study, real LLM monthly E2E report, third-party pen-test, 6-backend cluster failover drill, Helm chart. None of these were addressed in this pass. See `PRODUCTION_ASSESSMENT.md` for the full picture.

## Install

```bash
pip install universal-agent-memory==0.3.0
# Or with optional backends:
pip install "universal-agent-memory[all]==0.3.0"
```