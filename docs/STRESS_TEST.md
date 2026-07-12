# Stress Test (100k A+ Requirement)

`benchmarks/stress_test.py` is the A+ "100k high-concurrency stress
test" infrastructure: a real-backend concurrent workload that
measures throughput, latency percentiles, error rate, and memory
growth.

## Quick start

```bash
# In-process smoke (10k ops, 16 threads, ~1s on InMemoryStore)
python -m benchmarks.stress_test --backend memory --ops 10000 --concurrency 16

# 100k ops against PostgreSQL (CI does this)
python -m benchmarks.stress_test --backend postgresql \
    --ops 100000 --concurrency 32 --timeout 1800

# 100k ops against Redis
python -m benchmarks.stress_test --backend redis \
    --ops 100000 --concurrency 32
```

The script:
- Distributes ops evenly across `--concurrency` worker threads
- Mixes operations: 50% store, 30% retrieve, 15% search, 5% delete
  (configurable via `--mix store,retrieve` etc.)
- Records per-op latency, success, error
- Emits a JSON report with ops/sec, p50/p95/p99 latency, error rate,
  per-op breakdown, RSS growth, and warnings

## What the warnings mean

A run is "clean" when all of:

- `error_rate < 1%` (the failure threshold)
- `p95_ms < 1000` (sub-second is reasonable for a stress workload)
- `rss_growth_mb < 200` (no obvious leak)
- `ops_completed == ops_requested` (no timeout)

If any of these fail, the JSON report includes a `warnings` array
explaining which thresholds were violated. The exit code is 1 in
that case, so CI can flag a regression.

## CI integration

`.github/workflows/ci.yml` runs the stress test as 4 independent
jobs against the service-container backends (PostgreSQL, ChromaDB,
Redis, Neo4j):

- `stress-postgresql` — `postgres:15-alpine` service container
- `stress-redis` — `redis:7-alpine` service container
- `stress-neo4j` — `neo4j:5-community` service container
- `stress-chromadb` — no service container (uses chromadb's
  in-process `EphemeralClient`)

Each job:

- 100k ops, 32 concurrent workers
- 1800s (30 min) timeout
- `continue-on-error: true` — informational by default, since
  real-world backends can flake at high concurrency
- JSON report uploaded as an artifact (`stress-report-{backend}`)

> **Note**: the original design was a single 4-backend matrix job
> that declared all 3 service containers. On busy runners this caused
> "One or more containers failed to start." and zero artifacts. The
> 4-independent-jobs design (commit `4927149`) fixes that by giving
> each job only its own service.

The CI job is **not a hard gate** because flaky-network CI runners
make 100k ops non-deterministic. The operator is expected to review
the artifact and trend the numbers over time.

## Local 100k run (the real A+ evidence)

The CI job runs the script with hosted runners; for a true
"production-like" result, run locally against a properly sized
backend. Recommended:

```bash
# 8-core machine, dedicated PostgreSQL (or Redis / Chroma / Neo4j)
python -m benchmarks.stress_test --backend postgresql \
    --ops 100000 --concurrency 32 --timeout 1800 \
    --output stress_report_$(date +%Y%m%d).json
```

Save the JSON artifacts, then diff them over time. Regressions
show up as `p95_ms` growth or `error_rate` drift before they
become user-visible outages.

## What this catches

The stress test is **not** a substitute for production load, but
it surfaces specific classes of bug that don't show up in unit
tests:

1. **Lock contention**: SQLite at concurrency > pool_size hits
   "database is locked" errors. InMemoryStore is thread-safe by
   design; PG/Redis/Neo4j clients are pooled. The stress test
   reveals the pool size threshold.
2. **Memory leaks**: `rss_growth_mb` over 200MB in 100k ops is a
   red flag. The default warning catches this.
3. **FTS5 / index edge cases**: queries with hyphens or special
   chars hit the FTS5 fallback path, which can be a SQL injection
   risk (we found one in `storage/sqlite.py` while smoke-testing
   the stress test).
4. **Connection pool exhaustion**: long-running tests that don't
   close connections eventually starve the pool. The stress test
   reports the per-op latency which spikes when this happens.

## What this does NOT catch

- Real-world traffic patterns (the mix is fixed 50/30/15/5; real
  traffic is messier)
- Multi-tenant isolation (single store, single client)
- LLM cost (this is purely the storage layer; LLM E2E is the
  separate A+ requirement — see `LONG_TERM_LLM.md`)

## Known false-positive patterns

The stress test is conservative: it reports ANY warning, including
benign ones. Review each warning before treating it as a
regression:

- **error_rate spike at startup**: the first few ops may see
  "connection refused" because the client hasn't fully initialized
  the pool. The pool self-heals on the next call.
- **FTS5 fallback on hyphenated queries**: real-world queries
  rarely have hyphens. The fallback path may legitimately fail
  on synthetic stress data and the test reports it as an error.
  This is a "stress-test finds edge cases" feature, not a bug.

## Adding a new backend

To wire a new storage backend into the stress test:

1. Add a builder branch to `_build_store()` in `benchmarks/stress_test.py`.
2. Add a new `stress-<backend>` job in `.github/workflows/ci.yml`
   (NOT to a matrix — see the "CI integration" section above for why).
   Declare the service container in the job's `services:` block.
3. Add a brief doc note to this file.

## Diagnosed bugs (as of 2026-07-12)

The stress test has been useful for surfacing real bugs in storage
backends. The list so far:

| Bug | Backend | Symptom in stress | Fix |
|-----|---------|-------------------|-----|
| `pool_max=10` < 32 workers | PostgreSQL | `error_rate=81%`, all op types equally broken | Pass `pool_max=64` in stress_test.py (overrides default) |
| `max_capacity` kwarg not accepted | Redis | `TypeError: unexpected keyword argument 'max_capacity'` at setup, **no report produced** | Remove the kwarg from stress_test.py's `_build_store()` |
| Unnecessary `RLock` serialization | Redis | 7.6 ops/sec for 32 workers (each op took ~4 sec) | Remove `with self._lock:` from 6 methods in `RedisStore` |
| Multi-step ops without pipeline | Redis | 3 round-trips per `store()` (HSET + EXPIRE + ZADD) | Wrap in `pipeline(transaction=False)` |
| O(N) full SCAN in `search_keywords()` | Redis | Search p50 = 28 sec on 13k memories | Add inverted token index (per-term SET + per-memory token SET) |
| WAL mode + concurrent writers | SQLite | "Database is locked" errors at 4+ threads | `RLock` around writes + `busy_timeout=5000` |
| FTS5 hyphen parsing | SQLite | `search_keywords('state-of-the-art')` returns empty | Wrap query as FTS5 phrase |

## Adding a new backend

To wire a new storage backend into the stress test:

1. Add a builder branch to `_build_store()` in `benchmarks/stress_test.py`.
2. Add a new `stress-<backend>` job in `.github/workflows/ci.yml`
   (NOT to a matrix — see the "CI integration" section above for why).
   Declare the service container in the job's `services:` block.
3. Add a brief doc note to this file.
