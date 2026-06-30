# Architecture Documentation

## Table of Contents

- [System Overview](#system-overview)
- [Memory Loop](#memory-loop)
- [Four-Tier Memory Model](#four-tier-memory-model)
- [Storage Abstraction](#storage-abstraction)
- [Hybrid Retrieval](#hybrid-retrieval)
- [Privacy & Deduplication](#privacy--deduplication)
- [Forgetting Engine](#forgetting-engine)
- [Multi-Agent Coordination](#multi-agent-coordination)
- [Thread Safety](#thread-safety)
- [Error Handling](#error-handling)
- [Configuration System](#configuration-system)
- [Security Architecture](#security-architecture)
- [Performance Characteristics](#performance-characteristics)

---

## System Overview

UAMS is a domain-agnostic persistent memory layer for AI agents. It follows the **memory loop**:

```
Capture → Privacy Filter → Deduplication → Working Store
    ↓
Consolidation (on session end) → Compression → Episodic / Semantic / Procedural Stores
    ↓
Retrieval (BM25 + Vector + Graph) → Token Budget Compression → Injected into Agent Prompt
```

The system is designed to be:
- **Framework agnostic**: Works with any agent framework
- **Backend agnostic**: Supports 6 storage backends
- **Thread safe**: All operations are protected by RLock
- **Production ready**: Graceful degradation, retries, monitoring, security

---

## Memory Loop

### Ingestion Flow

```
AgentEvent
    ↓
EventBus.dispatch()
    ↓
PrivacyFilter.sanitize()      → Strip secrets, mask PII
    ↓
DeduplicationWindow.check()   → SHA-256 rolling hash, skip duplicates
    ↓
InMemoryStore (Working tier)  → 30min TTL, exact match retrieval
```

### Consolidation Flow (triggered on SESSION_END)

```
Working Store events
    ↓
CompressionEngine.compress()  → LLM or heuristic summarization
    ↓
    ├─→ Episodic Store        → 7d half-life, keyword + semantic
    ├─→ Semantic Store        → 90d half-life, vector search
    └─→ Procedural Store      → 1yr half-life, graph + pattern
```

### Retrieval Flow

```
User Query
    ↓
RetrievalPipeline.retrieve()
    ├─→ BM25 Keyword Search   → Fast, exact match
    ├─→ Vector Search          → Semantic similarity
    └─→ Graph Traversal        → Relationship-based
    ↓
RRF Fusion (Reciprocal Rank Fusion)
    ↓
TokenBudgetCompressor.compress()
    ↓
Format as prompt text block
    ↓
Inject into Agent Context Window
```

---

## Four-Tier Memory Model

UAMS models memory after human cognitive architecture:

```
┌────────────────────────────────────────────────────────────┐
│  WORKING     Raw events, sensory input          (30min TTL) │
│  ─────────────────────────────────────────────────────────  │
│  EPISODIC    Session narratives, experiences      (7d hl)   │
│  ─────────────────────────────────────────────────────────  │
│  SEMANTIC    Facts, preferences, concepts         (90d hl)│
│  ─────────────────────────────────────────────────────────  │
│  PROCEDURAL  Skills, workflows, patterns        (1yr hl)  │
└────────────────────────────────────────────────────────────┘
```

### Tier Characteristics

| Tier | Capacity | Search Speed | Retrieval Strategy | Decay Rate |
|------|----------|--------------|-------------------|------------|
| Working | 10K events | < 1ms | Exact match, recency | 30 min TTL |
| Episodic | 1K sessions | < 5ms | BM25 + recency | 7 days half-life |
| Semantic | 10K facts | < 20ms | Vector similarity | 90 days half-life |
| Procedural | 100 patterns | < 50ms | Graph traversal | 1 year half-life |

### Storage Mapping

Each tier can be mapped to a different backend:

```python
config = UAMSConfig(
    # Fast, ephemeral
    working_store=InMemoryStore(max_capacity=10000),
    
    # Persistent, searchable
    episodic_store=SQLiteStore(db_path="./episodic.db"),
    semantic_store=ChromaDBStore(persist_directory="./semantic"),
    
    # Graph-structured
    procedural_store=Neo4jStore(uri="bolt://localhost:7687"),
)
```

---

## Storage Abstraction

### MemoryStore Interface

All storage backends implement the `MemoryStore` abstract interface:

```python
class MemoryStore(ABC):
    @abstractmethod
    def store(self, memory: Memory) -> None: ...
    
    @abstractmethod
    def retrieve(self, memory_id: MemoryId) -> Optional[Memory]: ...
    
    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[Memory]: ...
    
    @abstractmethod
    def delete(self, memory_id: MemoryId) -> bool: ...
    
    @abstractmethod
    def delete_expired(self, before: float) -> int: ...
    
    @abstractmethod
    def list_all(self, limit: int = 100) -> List[Memory]: ...
```

### Backend Comparison

| Backend | Persistence | Concurrency | Search | Best For |
|---------|-------------|-------------|--------|----------|
| InMemory | ❌ | ⚠️ (RLock) | Dict lookup | Testing, prototyping |
| SQLite | ✅ | ✅ (WAL) | FTS5 | Single-node, embedded |
| PostgreSQL | ✅ | ✅ (MVCC) | GIN + JSONB | Enterprise, high-scale |
| Redis | ✅ | ✅ | ZSET | Distributed cache, signals |
| Neo4j | ✅ | ✅ | Cypher graph | Knowledge graphs, relationships |
| ChromaDB | ✅ | ✅ | Vector | Semantic search, embeddings |

---

## Hybrid Retrieval

### Three-Stream Retrieval

```python
class RetrievalPipeline:
    def retrieve(self, query, context, top_k=5):
        # Stream 1: BM25 Keyword Search
        keyword_results = self.keyword_search(query, top_k=top_k*2)
        
        # Stream 2: Vector Search
        vector_results = self.vector_search(query, top_k=top_k*2)
        
        # Stream 3: Graph Traversal (limited to 3 entities)
        graph_results = self.graph_search(query, entity_limit=3, top_k=top_k*2)
        
        # Fusion: Reciprocal Rank Fusion
        fused = self.rrf_fuse([keyword_results, vector_results, graph_results])
        
        # Budget: Token Budget Compression
        return self.token_budget_compress(fused, budget_tokens)
```

### RRF Formula

```python
score = sum(1.0 / (k + rank) for each_stream)
# k = 60 (constant)
# rank = position in that stream's results
```

### Token Budget Compression

```python
# Compress retrieved memories to fit within LLM token budget
compressed = []
remaining = budget_tokens

for memory in fused_results:
    tokens = self.estimate_tokens(memory.payload.raw)
    if tokens <= remaining:
        compressed.append(memory)
        remaining -= tokens
    else:
        # Truncate or skip
        truncated = self.truncate(memory, remaining)
        if truncated:
            compressed.append(truncated)
        break
```

---

## Privacy & Deduplication

### Privacy Filter

Uses regex patterns to detect and redact:
- API keys (OpenAI, AWS, etc.)
- Bearer tokens
- Email addresses
- Chinese phone numbers
- Credit card numbers (configurable)

```python
class PrivacyFilter:
    PATTERNS = [
        (r'sk-[a-zA-Z0-9]{48}', '[REDACTED_API_KEY]'),
        (r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[REDACTED_EMAIL]'),
        (r'1[3-9]\d{9}', '[REDACTED_PHONE]'),  # Chinese mobile
    ]
```

### Deduplication Window

Rolling SHA-256 window prevents duplicate ingestion within a configurable time window:

```python
class DeduplicationWindow:
    def __init__(self, window_seconds=30.0, max_window_size=1000):
        self.window = OrderedDict()  # hash → timestamp
        self.lock = RLock()
    
    def is_duplicate(self, content: str) -> bool:
        hash_val = hashlib.sha256(content.encode()).hexdigest()[:16]
        with self.lock:
            now = time.time()
            # Clean old entries
            self.window = {k: v for k, v in self.window.items() if now - v < self.window_seconds}
            # Check duplicate
            if hash_val in self.window:
                return True
            self.window[hash_val] = now
            return False
```

---

## Forgetting Engine

### Ebbinghaus Decay Formula

```
retention = 0.5^(age / half_life)
            × (1 + 0.1 × access_count)      # Access strengthens
            × (0.5 + 0.5 × importance/10)    # Importance persists
            × confidence                        # Contradiction fades
```

### Automatic Eviction

```python
def should_evict(memory, retention_floor=0.01):
    retention = calculate_retention(memory)
    return retention < retention_floor
```

### Background Cleanup

- `delete_expired()` is called periodically by a background thread
- Each store manages its own TTL or half-life
- Eviction is lazy (checked on retrieval) and proactive (background scan)

---

## Multi-Agent Coordination

### Architecture

```
┌─────────────────────────────────────────┐
│         MultiAgentCoordinator          │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │  Lock   │  │ Signal  │  │ Shared  │ │
│  │ Manager │  │  Queue  │  │ Memory  │ │
│  └─────────┘  └─────────┘  └─────────┘ │
└─────────────────────────────────────────┘
```

### Resource Leases

Exclusive locks with TTL to prevent deadlocks:

```python
class LockManager:
    def acquire(self, agent_id, resource_id, ttl=300.0) -> bool:
        # Redis: SET resource_key agent_id NX EX ttl
        # Memory: Dict[resource_id] = (agent_id, expiry_time)
```

### Signal Passing

Pub/Sub for inter-agent communication:

```python
class SignalBus:
    def send(self, signal: Signal) -> None:
        # Redis: PUBLISH channel signal_json
        # Memory: Append to agent's signal queue
    
    def read(self, agent_id) -> List[Signal]:
        # Read and clear unread signals
```

### Shared Memory Spaces

Team-based shared context:

```python
def share_memory(memory, target_team):
    # Store in team's shared store
    team_store = get_team_store(target_team)
    team_store.store(memory)
```

---

## Thread Safety

All shared state is protected by `threading.RLock`:

| Component | Lock Strategy |
|-----------|--------------|
| InMemoryStore | One RLock per store instance |
| EventBus | One RLock for buffer and subscribers |
| DeduplicationWindow | One RLock for the rolling window |
| MultiAgentCoordinator | One RLock for locks + signals + shared memory |
| SQLiteStore | Connection pooling with RLock per connection |
| PostgreSQLStore | ThreadedConnectionPool (thread-safe by psycopg2) |
| RedisStore | redis-py is thread-safe (connection pooling) |
| Neo4jStore | Neo4j driver handles thread safety |

---

## Error Handling

### Graceful Degradation Strategy

```python
try:
    embedding = self._embedding_fn(text)
except Exception as e:
    logger.warning("Embedding failed, falling back to keyword search: %s", e)
    embedding = None
    # Fallback to keyword search in retrieval
```

### Retry Policy

Exponential backoff with jitter:

```python
@retry(max_retries=3, base_delay=0.1, max_delay=10.0, exponential_base=2.0)
def call_embedding_api(text):
    return embedding_client.embed(text)
```

### Error Classification

| Error Type | Handling |
|-----------|----------|
| ImportError (optional deps) | Log warning, fallback to no-op |
| ConnectionError (network) | Retry with exponential backoff |
| ValueError (input) | Log error, return empty result |
| RuntimeError (critical) | Log error, degrade gracefully |

---

## Configuration System

### Validation Rules

```python
class UAMSConfig:
    def validate(self):
        errors = []
        if self.max_raw_length < 1:
            errors.append("max_raw_length must be >= 1")
        if self.memory_capacity < 1:
            errors.append("memory_capacity must be >= 1")
        if self.postgresql_pool_min >= self.postgresql_pool_max:
            errors.append("pool_min must be < pool_max")
        if not (1 <= self.postgresql_port <= 65535):
            errors.append("port must be in [1, 65535]")
        # ... 12+ total constraints
        if errors:
            raise ValueError(f"Validation failed: {'; '.join(errors)}")
```

### Environment Variables

All config fields are overridable via environment variables:

```bash
UAMS_STORAGE_BACKEND=postgresql
UAMS_POSTGRESQL_HOST=db.prod.local
UAMS_POSTGRESQL_PORT=5432
UAMS_MAX_RAW_LENGTH=50000
UAMS_LOG_LEVEL=WARNING
UAMS_TOKEN_BUDGET=4000
```

---

## Security Architecture

### Input Sanitization Pipeline

```
Raw Input
    ↓
sanitize_sql()      → Remove SQL keywords, dangerous chars
    ↓
sanitize_html()     → HTML escape, entity encoding
    ↓
truncate()          → Length limit enforcement
    ↓
Safe Input
```

### Rate Limiting

Sliding window rate limiter per key:

```python
class RateLimiter:
    def __init__(self, max_requests=5, window_seconds=60.0):
        self.windows = {}  # key → deque of timestamps
    
    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window = self.windows.get(key, deque())
        # Remove expired timestamps
        while window and now - window[0] > self.window_seconds:
            window.popleft()
        if len(window) < self.max_requests:
            window.append(now)
            self.windows[key] = window
            return True
        return False
```

---

## Performance Characteristics

### Benchmark Results (InMemoryStore, n=1000)

| Operation | ops/sec | avg (ms) | Notes |
|-----------|---------|----------|-------|
| Store | 50,000+ | < 0.02 | Single-threaded |
| Retrieve | 100,000+ | < 0.01 | Dict lookup |
| Search (keyword) | 10,000+ | < 0.1 | Simple substring |
| Search (vector) | 500+ | < 2 | With embedding generation |
| Delete expired | 5,000+ | < 0.2 | Batch scan |

### Scaling Guidelines

| Backend | Max Memory | Concurrent Agents | Latency |
|---------|-----------|-------------------|---------|
| InMemory | 10K memories | 1-10 | < 1ms |
| SQLite | 1M+ memories | 1-50 | < 5ms |
| PostgreSQL | 10M+ memories | 100+ | < 10ms |
| Redis | 100M+ memories | 1000+ | < 5ms |
| Neo4j | 10M+ nodes | 100+ | < 20ms |
| ChromaDB | 1M+ vectors | 10-50 | < 50ms |

---

For more details, see the [API Reference](API.md) and [Deployment Guide](DEPLOYMENT.md).
