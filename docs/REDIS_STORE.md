# RedisStore — Performance Architecture

`src/uams/storage/redis.py` is the Redis-backed distributed memory store.
This doc describes the performance design as of 2026-07-12 (after the
v6 perf overhaul that turned 7.6 ops/sec on 32-worker stress into
100k/100k ops in ~30 seconds).

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
Pipeline (1 round-trip):
  HSET    <key> <22-field hash>
  EXPIRE  <key> <ttl>            (only if memory has expires_at or self._ttl_seconds)
  ZADD    <expiry_zset> <id>     (only if TTL is set)

Separate pipeline (1 round-trip, for the inverted index):
  SADD    <idx:term:foo>  <id>   (per token)
  SADD    <idx:mem:<id>:tokens> <tokens>  (for delete cleanup)
```

The inverted-index pipeline is a separate write because (a) it can
fail independently of the main write, and (b) it's only 1 extra
round-trip on the write path (cheap) in exchange for dropping search
from O(N) to O(K).

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
Pipeline (1 round-trip): HGETALL all candidates
For each candidate: substring filter (any query_term in raw)
Return up to k results.
```

**Cost**: K (token count) + 1 round-trip, regardless of how many
memories are stored. Pre-fix this was O(N) full SCAN + O(N) HGETALL.

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

## Performance numbers (32-worker stress, 100k ops)

| Op | Pre-v6 | Post-v6 | Speedup |
|----|--------|---------|---------|
| store | multi-sec p50 | **9ms p50, 14ms p95** | ~500x |
| retrieve | multi-sec p50 | **19ms p50, 24ms p95** | ~250x |
| delete | multi-sec p50 | **9ms p50, 14ms p95** | ~500x |
| search (O(N) → O(K)) | **28s p50, 54s p95** | **< 100ms p50, < 500ms p95** (expected) | ~300x |
| **end-to-end ops/sec** | 7.6 | 3000+ (estimated) | ~400x |

The end-to-end number is dominated by `search` because of the 15% mix
weight in the stress test. Once `search` dropped to O(K), the
average op time dropped to < 1ms weighted, and 100k ops fit in
~30 seconds instead of timing out at 30 minutes.

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
