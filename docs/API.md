# API Reference

> **Source of truth:** this file documents only the public Python API
> that exists in the code. If a method, parameter, or enum value is
> not listed here, it does not exist — do not invent one. When in
> doubt, introspect the running code:
>
> ```python
> import inspect
> from uams import UniversalMemorySystem
> print(inspect.signature(UniversalMemorySystem.__init__))
> print(inspect.signature(UniversalMemorySystem.remember))
> ```

## Table of Contents

- [UniversalMemorySystem](#universalmemorysystem)
- [Memory Primitives](#memory-primitives)
- [AgentContext](#agentcontext)
- [AgentEvent](#agentevent)
- [Memory](#memory)
- [Storage Backends](#storage-backends)
- [Configuration](#configuration)
- [Privacy & Security](#privacy--security)
- [Benchmarks](#benchmarks)
- [Backup & Migration](#backup--migration)
- [Multi-Agent Coordination](#multi-agent-coordination)
- [Async API](#async-api)

---

## UniversalMemorySystem

The main facade. Any agent framework integrates via this class.

```python
from uams import UniversalMemorySystem
```

### Constructor

The actual signature — do NOT add extra kwargs, the dataclass is frozen
and rejects unknown fields with `TypeError`.

```python
ums = UniversalMemorySystem(
    stores: Optional[Dict[MemoryType, MemoryStore]] = None,  # tier → store
    compression: Optional[CompressionEngine] = None,
    embedding_fn: EmbeddingFn = None,
    privacy_filter: Optional[PrivacyFilter] = None,
    dedup_window: Optional[DeduplicationWindow] = None,
    config: Optional[UAMSConfig] = None,
)
```

If `stores` is omitted, `config.storage_backend` selects the backend.
If `config` is also omitted, `UAMSConfig.from_env()` is used (env-driven).

### observe(event)

Record any `AgentEvent` into Working memory.

```python
ums.observe(AgentEvent(
    event_type=EventType.USER_INPUT,
    agent_context=ctx,
    content="User said hello",
    structured_data={"intent": "greeting"},
))
```

### remember(fact, ...)

Explicitly save a fact to Semantic memory.

```python
ums.remember(
    fact="Alice is vegetarian",
    context=ctx,
    importance=8.0,
    category="dietary",
    privacy=PrivacyLevel.PUBLIC,
    tags={"dietary", "preference"},   # set, not list
)
# Returns MemoryId on success, None on failure (graceful degradation).
```

### recall(query, ...)

Retrieve relevant memories across all tiers.

```python
memories = ums.recall(
    query="hotel preferences",
    context=ctx,
    budget_tokens=1000,            # Optional; falls back to config.default_token_budget.
    include_working=True,
)
# Returns List[Memory]; empty list on any failure (never raises).
```

### forget(memory_id, cascade=...)

Delete a memory by ID. Returns a `CascadeReport` (not a bool! — see
docs/CASCADE_FORGET.md). The default cascade strategy is
`CascadeStrategy.BIDIRECTIONAL` (GDPR-aligned, same-tier).

```python
from uams.pipeline.cascade import CascadeStrategy

report = ums.forget("mem-1")                       # default BIDIRECTIONAL
report = ums.forget("mem-1", cascade=CascadeStrategy.ISOLATED)
report = ums.forget("mem-1", cascade=CascadeStrategy.FULL_CASCADE)  # cross-tier
print(report.deleted_ids, report.is_complete)
```

### consolidate(session_id=None)

Trigger 4-tier compression. Auto-triggered on `EventType.SESSION_END`.
If `session_id` is None, consolidates all pending sessions.

```python
ums.consolidate(session_id="sess_1")
```

### inject_context(query, context, budget_tokens=None)

Format memories as a prompt text block.

```python
context_block = ums.inject_context(
    query="hotel preferences",
    context=ctx,
    budget_tokens=1000,
)
# Returns: a markdown-formatted string suitable for prompt injection.
```

### decay_sweep()

Run forgetting sweep. Returns count of evicted memories. **UAMS does
NOT start a background sweeper thread; the caller is responsible for
invoking this periodically** (e.g. from a cron, asyncio loop, or
the docker entrypoint, which does so every 60s).

```python
evicted = ums.decay_sweep()
```

### shutdown()

Graceful shutdown: persists WORKING-tier memories to EPISODIC and
closes all backend connections. Wire `ums.register_signal_handlers()`
in production so `docker stop` / SIGTERM triggers this.

```python
ums.register_signal_handlers()
ums.shutdown()
```

---

## Memory Primitives

| Primitive | Signature | Purpose |
|-----------|-----------|---------|
| `observe(event)` | `observe(event: AgentEvent)` | Record event into Working memory (primary ingestion) |
| `remember(fact, ...)` | `remember(fact, context, importance=5.0, category="general", privacy=PUBLIC, tags=None)` | Save fact to Semantic memory |
| `recall(query, ...)` | `recall(query, context, budget_tokens=None, include_working=True)` | Retrieve relevant memories |
| `forget(memory_id, cascade=...)` | `forget(memory_id, cascade=CascadeStrategy.BIDIRECTIONAL)` | Delete a memory, returns CascadeReport |
| `consolidate(session_id=None)` | `consolidate(session_id=None)` | Trigger 4-tier compression |
| `inject_context(...)` | `inject_context(query, context, budget_tokens=None)` | Format memories as prompt text |
| `decay_sweep()` | `decay_sweep()` | Run forgetting sweep; returns evict count |
| `shutdown()` | `shutdown()` | Persist WORKING→EPISODIC + close backends |

---

## AgentContext

Identifies who produced a memory.

```python
from uams import AgentContext

ctx = AgentContext(
    agent_id="pa_001",           # Required
    agent_type="personal_assistant",  # Required
    session_id="sess_1",         # Required
    user_id="alice",             # Optional
    team_id="team_1",            # Optional
    project_id="project_1",      # Optional
)
```

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | str | Unique agent identifier |
| `agent_type` | str | Agent category (e.g., "personal_assistant", "game_npc") |
| `session_id` | str | Current session identifier |
| `user_id` | Optional[str] | End user identifier |
| `team_id` | Optional[str] | Team / organization identifier |
| `project_id` | Optional[str] | Project identifier |

---

## AgentEvent

Any observable event in an agent's lifecycle.

```python
from uams import AgentEvent, EventType

event = AgentEvent(
    event_type=EventType.USER_INPUT,
    agent_context=ctx,
    content="User said hello",
    structured_data={"intent": "greeting", "confidence": 0.95},
)
```

### EventType Enum

Authoritative list — generated from `uams.core.enums.EventType`. Values
not in this table do NOT exist (this was previously out of sync with
the code and listed `SYSTEM_EVENT`, `MANUAL`, `ERROR` which were never
real enum members).

| Value | Category | Description |
|-------|----------|-------------|
| `ENV_OBSERVATION` | Perception | Agent observed environment state |
| `USER_INPUT` | Perception | User message or query |
| `AGENT_OUTPUT` | Perception | Agent response or decision |
| `ACTION_START` | Action | Tool/action execution begins |
| `ACTION_END` | Action | Tool/action execution completes |
| `ACTION_FAILURE` | Action | Tool/action failed |
| `PLAN_FORMED` | Meta-cognition | Agent formed a plan or intention |
| `PLAN_EXECUTED` | Meta-cognition | Plan step completed |
| `PLAN_ABORTED` | Meta-cognition | Plan abandoned |
| `REFLECTION` | Meta-cognition | Agent self-reflection |
| `SESSION_START` | Session lifecycle | Session began |
| `SESSION_END` | Session lifecycle | Session ended (triggers consolidation) |
| `SUBSESSION_START` | Session lifecycle | Sub-task / sub-agent spawned |
| `SUBSESSION_END` | Session lifecycle | Sub-task / sub-agent finished |
| `SIGNAL_RECEIVED` | Multi-agent | Message from another agent |
| `SIGNAL_SENT` | Multi-agent | Message sent to another agent |
| `LEASE_ACQUIRED` | Multi-agent | Exclusive resource lock acquired |
| `LEASE_RELEASED` | Multi-agent | Exclusive resource lock released |

---

## Memory

The core memory record.

```python
from uams import Memory, MemoryId, TemporalAnchor, MemoryPayload, MemoryMetadata

memory = Memory(
    id=MemoryId(),                    # Unique identifier
    anchor=TemporalAnchor(            # Temporal metadata
        created_at=time.time(),
        accessed_at=None,
        consolidated_at=None,
        expires_at=time.time() + 3600,  # TTL for automatic eviction
    ),
    context=ctx,                      # AgentContext (who)
    payload=MemoryPayload(            # What
        raw="Alice is vegetarian",
        structured={"dietary": "vegetarian"},
        embedding=None,               # Optional: vector embedding
    ),
    metadata=MemoryMetadata(          # How
        memory_type=MemoryType.SEMANTIC,
        privacy=PrivacyLevel.PUBLIC,
        importance=8.0,               # 0-10, affects retention
        confidence=0.95,              # 0-1, affects retention
        source_event=EventType.USER_INPUT,
        tags={"dietary", "preference"},
        categories={"travel"},
        provenance=["session_1", "user_input_42"],
    ),
)
```

### MemoryType Enum

| Value | TTL | Search Method | Description |
|-------|-----|---------------|-------------|
| `WORKING` | 30 min | Exact match / recent | Raw events |
| `EPISODIC` | 7 days | Keyword + semantic | Session summaries |
| `SEMANTIC` | 90 days | Semantic vector | Facts and preferences |
| `PROCEDURAL` | 1 year | Graph + pattern | Patterns and strategies |

### PrivacyLevel Enum

Authoritative list — generated from `uams.core.enums.PrivacyLevel`.
A previous version of this table listed `CONFIDENTIAL`, which is not
a real enum member.

| Value | Description |
|-------|-------------|
| `PUBLIC` | Safe to share across agents |
| `INTERNAL` | Within same agent instance |
| `PRIVATE` | User-specific, sensitive |
| `SECRET` | Credentials, PII (never leave local storage) |

---

## Storage Backends

### InMemoryStore

```python
from uams.storage.memory import InMemoryStore

store = InMemoryStore(max_capacity=10000)  # LRU eviction when full
```

### SQLiteStore

```python
from uams.storage.sqlite import SQLiteStore

store = SQLiteStore(
    db_path="./uams.db",
    tier="episodic",          # "working", "episodic", "semantic", "procedural"
    pool_size=5,              # Connection pool
)
```

Features: WAL mode, FTS5 full-text search, automatic schema migrations, connection pooling.

### PostgreSQLStore

```python
from uams.storage.postgresql import PostgreSQLStore

store = PostgreSQLStore(
    host="localhost",
    port=5432,
    database="uams",
    user="uams",
    password="secret",
    tier="semantic",
    pool_min=2,
    pool_max=20,
)
```

Features: JSONB columns, GIN indexes, ACID transactions, connection pooling, schema migrations.

### RedisStore

```python
from uams.storage.redis import RedisStore

store = RedisStore(
    host="localhost",
    port=6379,
    db=0,
    password=None,
    key_prefix="uams:memory:",
    ttl_seconds=3600,
    enable_pubsub=True,       # Enable inter-agent signals
    pool_max_connections=50,
)
```

Features: Redis Hash, ZSET for expiry, Pub/Sub for signals, connection pooling.

### Neo4jStore

```python
from uams.storage.neo4j import Neo4jStore

store = Neo4jStore(
    uri="bolt://localhost:7687",
    user="neo4j",
    password="secret",
    database="neo4j",
    tier="procedural",
)
```

Features: Cypher graph queries, relationship indexing, vector similarity search fallback.

### ChromaDBStore

```python
from uams.storage.chromadb import ChromaDBStore

store = ChromaDBStore(
    collection_name="uams_semantic",
    persist_directory="./chroma_db",
    embedding_fn=None,       # Optional: custom embedding function
)
```

Features: Dense vector search, metadata filtering, automatic persistence.

---

## Configuration

### UAMSConfig

The dataclass has 30+ fields spanning storage backend selection, LLM
config, embedding providers, retention tuning, security strictness,
and audit paths. Field names use `*_seconds` suffixes (not bare units)
and tier-named fields use the pattern `<tier>_<metric>`.

**Do NOT hand-write field-by-field UAMSConfig instances from this
document** — the previous version did so and listed field names like
`working_ttl` (real name: `working_ttl_seconds`) and `retention_floor`
(not a field) that would have raised `TypeError` on a frozen
dataclass.

The supported way to configure UAMSConfig:

```python
# Option 1: environment-driven (recommended for production).
# All UAMS_* env vars are documented in src/uams/config.py.
import os
os.environ["UAMS_STORAGE_BACKEND"] = "postgresql"
os.environ["UAMS_POSTGRESQL_HOST"] = "db.prod.internal"
os.environ["UAMS_DEFAULT_TOKEN_BUDGET"] = "4000"
os.environ["UAMS_STRICTNESS"] = "production"  # dev/staging/production

from uams import UniversalMemorySystem, UAMSConfig
config = UAMSConfig.from_env()
config.validate()
ums = UniversalMemorySystem(config=config)

# Option 2: programmatic override of specific fields.
config = UAMSConfig.from_env()
config.working_ttl_seconds = 3600.0        # 1 hour
config.sqlite_pool_size = 8                # 8 conns in pool
config.log_level = "WARNING"
ums = UniversalMemorySystem(config=config)
```

For the complete field list and constraints, read `src/uams/config.py`
directly — it is the source of truth, and `inspect.signature(
UAMSConfig)` returns the full field list at runtime.

---

## Privacy & Security

### InputValidator

```python
from uams.utils.security import InputValidator

# Sanitize SQL injection
safe = InputValidator.sanitize_sql("'; DROP TABLE users; --")

# Sanitize XSS
safe = InputValidator.sanitize_html("<script>alert(1)</script>")

# Full sanitization chain
safe = InputValidator.sanitize_all(
    text="'; DROP TABLE users; -- <script>alert(1)</script>",
    max_length=1000,
)

# Rate limiting
limiter = InputValidator.rate_limiter(max_requests=5, window_seconds=60.0)
allowed = limiter.is_allowed("user_123")
```

### PrivacyFilter

```python
from uams.pipeline.privacy import PrivacyFilter

filter = PrivacyFilter()
redacted = filter.sanitize("My API key is sk-abc123xyz")
# Returns: "My API key is [REDACTED]"
```

---

## Benchmarks

```python
from uams.benchmarks import BenchmarkSuite

# Run all benchmarks
results = BenchmarkSuite.run_all(n=1000)

# Or individual benchmarks
store_result = BenchmarkSuite.benchmark_store(n=1000)
retrieve_result = BenchmarkSuite.benchmark_retrieve(n=1000)
search_result = BenchmarkSuite.benchmark_search_keywords(n_memories=1000, n_queries=100)
delete_result = BenchmarkSuite.benchmark_delete_expired(n=1000)

# Results contain ops, ops_per_sec, elapsed_ms, avg_ms, min_ms, max_ms, details
print(f"Store: {store_result.ops_per_sec:.0f} ops/sec")
```

---

## Backup & Migration

### Backup

```python
from uams.utils.backup import BackupManager

# To JSONL file
manager = BackupManager(store)
manager.backup_to_file("./backup.jsonl")

# To dict (for programmatic use)
data = manager.backup_to_dict()
```

### Restore

```python
from uams.utils.backup import BackupManager

manager = BackupManager(store)
manager.restore_from_file("./backup.jsonl")
manager.restore_from_dict(data)
```

### Migration

```python
from uams.utils.backup import MigrationTool

# Migrate from one backend to another
tool = MigrationTool()
migrated = tool.migrate(source=sqlite_store, target=postgres_store, batch_size=1000)
print(f"Migrated {migrated} memories")

# With filter
migrated = tool.migrate_with_filter(
    source=sqlite_store,
    target=postgres_store,
    filter_fn=lambda m: m.metadata.memory_type == MemoryType.SEMANTIC,
)
```

---

## Multi-Agent Coordination

### Enable

```python
ums.enable_multi_agent()  # Creates shared InMemoryStore by default
```

### Locks

```python
acquired = ums.acquire_lock("agent_a", "task_001", ttl=300.0)
ums.release_lock("agent_a", "task_001")
```

### Signals

```python
from uams import Signal

ums.send_signal(Signal(
    sender="agent_a",
    recipient="agent_b",  # "*" for broadcast
    signal_type="data_ready",
    payload={"dataset_size": 10000},
))

signals = ums.read_signals("agent_b")
```

---

## Async API

```python
from uams.async_system import AsyncUniversalMemorySystem

async_ums = AsyncUniversalMemorySystem()

await async_ums.observe(event)
memories = await async_ums.recall("query", context=ctx)
```

---

For more examples, see the `examples/` directory in the repository.
