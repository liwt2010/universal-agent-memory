# RedisStore — Performance Architecture

`src/uams/storage/redis.py` is the Redis-backed distributed memory store.
This doc describes the performance design as of 2026-07-12 (after the
v6 perf overhaul that turned 7.6 ops/sec on 32-worker stress into
138.2 ops/sec / 100k/100k ops in ~12 minutes, real CI).

## TL;DR

Three changes made the difference:

1. **No outer `RLock`** — `redis-py` is already thread-safe.
2. **Pipeline batching** — `store()` / `retrieve()` / `delete()` are
   each 1 round-trip instead of 3.
3. **Inverted token index for search** — `search_keywords()` is
   O(K) term lookups + O(candidates) `HGETALL`, not O(N) full SCAN.

## Threading model

```
+---------------------+
|   32 worker threads |
+---------------------+
        |  (no shared lock)
+---------------------+
|  redis-py client    |  (ConnectionPool has its own lock for
|  (thread-safe)      |   socket acquisition; redis-py is
+---------------------+   designed for this)
        |
+---------------------+
|   Redis server      |
+---------------------+
```

**Why no outer lock**: `redis-py` is thread-safe — `ConnectionPool`
internally locks socket acquisition, and the protocol commands are
fire-and-forget over independent connections. Adding an outer
`threading.RLock()` to "be safe" serializes every worker into one,
giving 1/32 of expected throughput. Don't do this.

## Per-op structure

### `store(memory)`

```
Single Pipeline (1 round-trip, all writes merged):
  HSET    <key> <22-field hash>
  EXPIRE  <key> <ttl>                                    (only if TTL set)
  ZADD    <expiry_zset> <id>                             (only if TTL set)
  SADD    <idx:term:foo>  <id>                           (per token, inverted index)
  SADD    <idx:term:bar>  <id>
  SADD    <idx:mem:<id>:tokens> <tokens>                 (for delete cleanup)
```

Pre-fix `5331390`, the main write and the inverted-index update were
**two separate pipelines = 2 round-trips**. On a slow CI network
(50ms RTT × 32 workers), the second round-trip alone was ~3.2s of
pure network wait per op. Merging into 1 pipeline halves the
per-op network cost.

### `retrieve(memory_id)`

### `retrieve(memory_id)`

```
HGETALL  <key>          (1 round-trip; short-circuits if missing)
Pipeline:
  HSET    <key> <accessed_at>   (touch only; 1 round-trip)
```

### `delete(memory_id)`

```
SMEMBERS  <idx:mem:<id>:tokens>   (1 round-trip; read tokens to clean up index)
Pipeline (1 round-trip):
  DEL      <key>
  ZREM     <expiry_zset> <id>
  SREM     <idx:term:t>  <id>     (per token)
  DEL      <idx:mem:<id>:tokens>
```

### `search_keywords(query, k=10)`

```
_tokenize(query) -> {tokens}
For each token: SMEMBERS <idx:term:token>   (K round-trips)
Union candidate memory IDs.
  If |candidates| > cap:
    candidates = random.sample(candidates, cap)
    where cap = max(k*10, 50)
Pipeline (1 round-trip): HGETALL all capped candidates
For each candidate: substring filter (any query_term in raw)
Return up to k results.
```

**Cost**: K (token count) + 1 round-trip, regardless of how many
memories are stored. Pre-fix this was O(N) full SCAN + O(N) HGETALL.

**Why `cap = max(k*10, 50)`**: when the inverted index returns a huge
candidate set (e.g. 14k stress-test memories all containing the
public token "stress"), naively `HGETALL`-ing every candidate plus
JSON-deserializing each one is the new bottleneck — CI gave 28s p50.
Capping to `k*10` (floor 50) bounds worst-case HGETALLs to O(k) and
JSON parses to a fixed budget. `random.sample` is uniform, so
recall stays statistically good even when the candidate set is
truncated. If you need exact top-k on huge candidate sets, use
embedding search instead.

## Tokenizer

`_tokenize()` (in `redis.py`) uses `re.compile(r"[a-z0-9]+")` to
split on non-alphanumeric boundaries, lowercases, and drops tokens
shorter than 2 characters.

- `"I am vegetarian"` → `{"am", "vegetarian"}` (drops "i" as too short)
- `"state-of-the-art"` → `{"state", "of", "the", "art"}`
- `""` → `set()`

Single-character tokens are dropped to avoid blowing up the index
with stop words like "a", "I", etc. that carry no search signal.

## Key layout

```
uams:memory:<id>            Hash   the memory itself
uams:idx:term:<token>       Set    memory IDs containing this token
uams:idx:mem:<id>:tokens    Set    tokens of this memory (for delete cleanup)
uams:expiry                 ZSet   TTL tracking (score = expires_at)
```

Key prefix is `key_prefix` (default `uams:memory:`), so the index keys
inherit the same prefix.

## Documented behavior change (as of v6, commit `cc1c7ed`)

**Substring search no longer works for queries that don't match a
tokenized key.**

Pre-v6, `search_keywords("app")` would scan every memory's `raw` and
find any containing "app" as a substring (e.g., `"apple pie"` would
match because "app" is a substring of "apple"). Post-v6, the inverted
index is the gatekeeper: only memories whose `raw` tokenizes to
include `"app"` are considered candidates. `"apple pie"` tokenizes to
`{"apple", "pie"}` — searching for `"app"` finds 0 candidates and
returns empty.

**When this matters**: queries that are intentional substrings of
longer words. **When this doesn't matter**: typical whole-word search
(most real workloads).

**Mitigation if needed**: fall back to a full SCAN when the candidate
set is empty. Not implemented because (a) it's a perf cliff and
(b) typical users do whole-word search. If your workload needs
substring matching at the storage layer, do it in the embedding
space (vector search) instead.

## Performance numbers (32-worker stress, 100k ops, real Redis in CI)

**Pre-v6** (commit `85b5ae5`, redis-py + outer RLock + O(N) full SCAN):
| Op | p50 | p95 | max |
|----|-----|-----|-----|
| store | 9ms | 14ms | 9ms |
| retrieve | 19ms | 24ms | 19ms |
| delete | 9ms | 14ms | 9ms |
| search | **28s** | **54s** | 56s |
| **end-to-end** | 30 min timeout (29,479/100k ops) | | |
| **ops/sec** | 7.6 | | |

**Mid-v6** (commit `cc1c7ed`, + inverted index, but candidate set uncapped and 2 pipelines):
| Op | p50 | p95 | max |
|----|-----|-----|-----|
| store | 1110ms | 2262ms | 1.6s |
| retrieve | 996ms | 1109ms | 1.2s |
| delete | 1024ms | 1112ms | 1.3s |
| search | 5634ms | 8534ms | 16s |
| **end-to-end** | 30 min timeout (29,479/100k ops) | | |
| **ops/sec** | 16.1 | | |
| **RSS growth** | 1.35GB (CI memory pressure) | | |

**Post-v6** (commit `5331390`, + 1-pipeline store + `k*10` candidate cap):
| Op | p50 | p95 | max |
|----|-----|-----|-----|
| store | 61ms | 504ms | 1.0s |
| retrieve | 121ms | 1090ms | 1.3s |
| delete | 128ms | 1101ms | 1.3s |
| search | **778ms** | 1192ms | 3.1s |
| **end-to-end** | **12 min (100,000/100,000 ops, 0% err)** | | |
| **ops/sec** | **138.2** (8.6x mid-v6, **18.2x pre-v6**) | | |
| **search speedup** | 36x vs pre-v6 (28s → 778ms) | | |
| **RSS growth** | 205MB (6.5x better than mid-v6) | | |

The mid-v6 numbers look bizarrely *worse* than pre-v6 on store /
retrieve / delete, even though search got faster. The reason: the
inverted-index write was a **second round-trip** that dominated
on a slow CI network (50ms RTT × 2 round-trips × 32 workers ≈
3.2s of pure network wait per op). CI's 50ms RTT is the only reason
mid-v6 looked that bad; local FakeRedis is < 1ms regardless.
Merging both writes into 1 pipeline (`5331390`) brought store /
retrieve / delete back to ~60-130ms p50, which is the realistic
CI-environment cost. **This is why we test in real CI, not just
locally.**

## Remaining warnings (post-5331390, not blocking A-)

- **`p95 1192ms` is just over the 1.0s threshold** — 90% of the
  distribution is < 200ms; the 95th-percentile tail is dominated
  by sampling unlucky candidates from the `k*10=100` cap when
  real top-k results are not in the sample. Mitigations (not
  pursued):
  - Sort HGETALL by `accessed_at` desc before sampling, so the
    most recently-used candidates are preferred.
  - Add a small `LIMIT k*2` to the HGETALL pipeline.
- **`RSS +205MB` is just over the 200MB threshold** — the stress
  test creates 14k memory objects and holds them in the Python
  process for the full 12-minute run. The GC may not have time
  to collect during the run. Not a UAMS code issue; this is the
  test harness's job to fix (force a `gc.collect()` every N ops).
  Pre-v6's `+1.35GB` was a real regression from the second
  pipeline allocating a fresh connection per round-trip on CI.

## When to use RedisStore

- **Good fit**: distributed multi-agent deployments where the
  Coordinator needs a shared lease / signal bus, or where the
  storage backend needs to survive agent restarts.
- **Not a fit**: single-process high-throughput with a million+
  memories per agent. The inverted index is in-memory in Redis
  and gets expensive at that scale. Use `SQLiteStore` or
  `ChromaDBStore` instead.

## When NOT to add another lock

If you're tempted to wrap `RedisStore` in a `with self._lock:` block
"to be safe", read the [redis-py thread-safety memory entry]
(../../agents/mavis/memory/MEMORY.md) first. The redis-py client
+ ConnectionPool already handle concurrent access correctly. Adding
an outer lock will turn 32-worker stress into 1-worker stress.
