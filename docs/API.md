# API Reference

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

---

## UniversalMemorySystem

The main facade. Any agent framework integrates via this class.

```python
from uams import UniversalMemorySystem
```

### Constructor

```python
ums = UniversalMemorySystem(
    backend: str = "memory",  # "memory", "sqlite", "postgresql", "redis", "neo4j", "chromadb"
    max_raw_length: int = 10000,
    retention_floor: float = 0.01,
    log_level: str = "INFO",
    token_budget: int = 2000,
    embedding_fn: Optional[Callable] = None,
)
```

Or use `UAMSConfig`:

```python
from uams import UniversalMemorySystem, UAMSConfig

config = UAMSConfig(
    storage_backend="postgresql",
    max_raw_length=50000,
    token_budget=4000,
)
config.validate()  # validates all constraints

ums = UniversalMemorySystem(config=config)
```

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
    memory_type=MemoryType.SEMANTIC,
    importance=8.0,
    confidence=0.95,
    tags={"dietary", "preference"},
)
```

### recall(query, ...)

Retrieve relevant memories across all tiers.

```python
memories = ums.recall(
    query="hotel preferences",
    context=ctx,
    budget_tokens=1000,
    top_k=5,
)
```

### forget(memory_id)

Delete a specific memory by ID.

```python
ums.forget(memory_id)
```

### consolidate(session_id)

Trigger 4-tier compression. Auto-triggered on `EventType.SESSION_END`.

```python
ums.consolidate(session_id="sess_1")
```

### inject_context(...)

Format memories as a prompt text block.

```python
context_block = ums.inject_context(
    query="hotel preferences",
    context=ctx,
    budget_tokens=1000,
)
# Returns: "## Relevant Memory Context\n1. [SEMANTIC] ...\n2. [EPISODIC] ..."
```

### sync(target)

Bidirectional sync with external files.

```python
ums.sync("./MEMORY.md")
```

---

## Memory Primitives

| Primitive | Signature | Purpose |
|-----------|-----------|---------|
| `observe(event)` | Record any `AgentEvent` into Working memory | Primary ingestion |
| `remember(fact, ...)` | Save a fact to Semantic memory | Explicit storage |
| `recall(query, ...)` | Retrieve relevant memories | Pre-turn context loading |
| `forget(memory_id)` | Delete a memory by ID | User request / GDPR |
| `consolidate(session_id)` | Trigger 4-tier compression | Auto on session end |
| `inject_context(...)` | Format memories as prompt text | Direct LLM injection |
| `sync(target)` | Sync with external files | External persistence |

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

| Value | Description |
|-------|-------------|
| `USER_INPUT` | User message or action |
| `AGENT_OUTPUT` | Agent response |
| `ENV_OBSERVATION` | Environment state change |
| `SYSTEM_EVENT` | System-level event |
| `SESSION_START` | Session began |
| `SESSION_END` | Session ended (triggers consolidation) |
| `ERROR` | Error occurred |
| `MANUAL` | Explicitly inserted memory |

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

| Value | Description |
|-------|-------------|
| `PUBLIC` | No restriction |
| `INTERNAL` | Within team only |
| `CONFIDENTIAL` | Agent only |
| `SECRET` | Requires explicit access |

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

```python
from uams import UAMSConfig

config = UAMSConfig(
    # Core
    storage_backend="postgresql",
    max_raw_length=10000,
    memory_capacity=10000,
    retention_floor=0.01,
    
    # Temporal
    working_ttl=1800.0,
    episodic_half_life=604800.0,
    semantic_half_life=7776000.0,
    procedural_half_life=31536000.0,
    
    # Storage
    sqlite_path="./uams.db",
    
    # PostgreSQL
    postgresql_host="localhost",
    postgresql_port=5432,
    postgresql_database="uams",
    postgresql_user="uams",
    postgresql_password="",
    postgresql_pool_min=2,
    postgresql_pool_max=20,
    
    # Redis
    redis_host="localhost",
    redis_port=6379,
    redis_db=0,
    redis_password=None,
    redis_key_prefix="uams:memory:",
    redis_ttl_seconds=None,
    redis_enable_pubsub=False,
    redis_pool_max_connections=50,
    
    # Neo4j
    neo4j_uri="bolt://localhost:7687",
    neo4j_user="neo4j",
    neo4j_password="",
    neo4j_database="neo4j",
    
    # Logging
    log_level="INFO",
    
    # System
    token_budget=2000,
    event_bus_max_buffer=10000,
    histogram_max_entries=10000,
    
    # Multi-agent
    multi_agent_enabled=False,
)

config.validate()  # Raises ValueError if any constraint is violated
```

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
