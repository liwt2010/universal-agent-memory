# UAMS Deployment & Operations Guide

## Table of Contents

- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [Storage Backend Selection](#storage-backend-selection)
- [Docker Deployment](#docker-deployment)
- [Health Checks & Monitoring](#health-checks--monitoring)
- [Multi-Agent Distributed Deployment](#multi-agent-distributed-deployment)
- [Performance Tuning](#performance-tuning)
- [Troubleshooting](#troubleshooting)

---

## Quick Start

> **v0.5.2 note**: this release ships `py.typed` (PEP 561), so any
> downstream project that runs `mypy` or `pyright` on its code that
> imports from `uams.*` will type-check against the real `uams`
> type signatures. No `py.typed` setup is needed on the consumer
> side — the marker travels with the wheel.
>
> **v0.5.2 has zero runtime change** versus v0.5.1. No migration
> steps are needed; any deployment scripts that pinned
> `universal-agent-memory>=0.5,<0.6` continue to work. If you were
> on v0.5.0, see `RELEASE_NOTES_v0.5.1.md` for the async-forget
> return-type change.

### 1. In-Memory Mode (Development / Testing)

```bash
pip install -e .
python -c "
from uams.system import UniversalMemorySystem
from uams.health import HealthServer

ums = UniversalMemorySystem()
server = HealthServer(port=3111)
server.start(ums_instance=ums)
print('UAMS running on http://localhost:3111')
"
```

### 2. SQLite Persistence (Single-Node Production)

```bash
export UAMS_STORAGE_BACKEND=sqlite
export UAMS_SQLITE_PATH=/data/uams.db
export UAMS_LOG_LEVEL=INFO

python -c "
from uams.system import UniversalMemorySystem
from uams.health import HealthServer

ums = UniversalMemorySystem()
server = HealthServer(port=3111)
server.start(ums_instance=ums)
"
```

### 3. Docker Compose One-Click Launch

```bash
docker compose up -d
```

Defaults to SQLite backend with data persisted to the `uams-data` volume.

---

## Environment Variables

| Variable | Default | Description |
|---------|---------|-------------|
| `UAMS_STORAGE_BACKEND` | `memory` | Backend: `memory` / `sqlite` / `redis` / `neo4j` |
| `UAMS_SQLITE_PATH` | `uams.db` | SQLite database file path |
| `UAMS_REDIS_HOST` | `localhost` | Redis host address |
| `UAMS_REDIS_PORT` | `6379` | Redis port |
| `UAMS_REDIS_DB` | `0` | Redis database index |
| `UAMS_REDIS_PASSWORD` | — | Redis password (optional) |
| `UAMS_REDIS_PREFIX` | `uams:memory:` | Redis key prefix |
| `UAMS_REDIS_TTL` | — | Global Redis TTL in seconds (optional) |
| `UAMS_REDIS_PUBSUB` | `false` | Enable Redis Pub/Sub signal queue |
| `UAMS_NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt URI |
| `UAMS_NEO4J_USER` | `neo4j` | Neo4j username |
| `UAMS_NEO4J_PASSWORD` | `password` | Neo4j password |
| `UAMS_NEO4J_DATABASE` | `neo4j` | Neo4j database name |
| `UAMS_LOG_LEVEL` | `INFO` | Log level: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `UAMS_HEALTH_PORT` | `3111` | Health check HTTP port |
| `UAMS_EVENT_BUS_MAX_BUFFER` | `1000` | Event bus buffer size |
| `UAMS_WORKING_TTL` | `1800` | Working tier TTL in seconds |
| `UAMS_RRF_K` | `60` | RRF fusion parameter K |

---

## Storage Backend Selection

| Backend | Use Case | Persistence | Multi-Process | Graph Traversal | Vector Search | Signal Queue |
|---------|----------|-------------|---------------|-----------------|---------------|--------------|
| **memory** | Dev / Test / Demo | ❌ | ❌ | ❌ | ❌ | ❌ |
| **sqlite** | Single-node / Small scale | ✅ | ❌ | Limited | ❌ | ❌ |
| **redis** | Distributed cache / High concurrency | ✅ | ✅ | ❌ | ❌ | ✅ |
| **neo4j** | Complex relations / Knowledge graph | ✅ | ✅ | ✅ | ❌ | ❌ |
| **chromadb** | Vector semantic retrieval | ✅ | ✅ | ❌ | ✅ | ❌ |

### Recommended Combinations

- **Single-node full-featured**: `sqlite` + `InMemoryStore(WORKING)`
- **Distributed cache**: `redis` (all tiers) + periodic `decay_sweep()`
- **Knowledge graph**: `neo4j` (all tiers) + graph relation modeling
- **Semantic retrieval**: `chromadb` (SEMANTIC tier) + `sqlite` (EPISODIC / PROCEDURAL)

### Switching Backends Example

```bash
# Redis backend
export UAMS_STORAGE_BACKEND=redis
export UAMS_REDIS_HOST=redis.cluster.local
export UAMS_REDIS_PORT=6380
export UAMS_REDIS_PUBSUB=true

# Neo4j backend
export UAMS_STORAGE_BACKEND=neo4j
export UAMS_NEO4J_URI=bolt://neo4j.prod:7687
export UAMS_NEO4J_PASSWORD=secure_password
```

---

## Docker Deployment

### Basic Deployment (SQLite)

```bash
docker compose up -d
```

### Redis Backend Deployment

```bash
docker compose -f docker-compose.yml -f docker-compose.redis.yml up -d
```

### Neo4j Backend Deployment

```bash
docker compose -f docker-compose.yml -f docker-compose.neo4j.yml up -d
```

### Custom Image Build

```bash
docker build -t uams:latest .
docker run -d \
  -p 3111:3111 \
  -e UAMS_STORAGE_BACKEND=sqlite \
  -e UAMS_SQLITE_PATH=/data/uams.db \
  -v uams-data:/data \
  uams:latest
```

---

## Health Checks & Monitoring

### Health Endpoints

| Endpoint | Description | Example |
|----------|-------------|---------|
| `GET /health` | System health | `{"status": "healthy"}` |
| `GET /ready` | Readiness check | `{"status": "ready"}` |
| `GET /metrics` | Prometheus metrics | `uams_requests_total 42` |
| `GET /stats` | Memory stats | `{"WORKING": 5, "EPISODIC": 12}` |

### Container Health Check

Built into the Dockerfile:
```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3111/health')" || exit 1
```

### Custom Monitoring Integration

```python
from uams.health import MetricsCollector

collector = MetricsCollector()
collector.increment("custom_event")
collector.histogram("latency_ms", 45.2)

# Get Prometheus-formatted output
print(collector.format_prometheus())
```

---

## Multi-Agent Distributed Deployment

### Architecture Overview

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Agent A   │◄───►│   Redis     │◄───►│   Agent B   │
│  (Worker)   │     │ (Pub/Sub)   │     │  (Worker)   │
└─────────────┘     └─────────────┘     └─────────────┘
                            │
                     ┌──────┴──────┐
                     │  SQLite/    │
                     │   Neo4j     │
                     │ (Persistent)│
                     └─────────────┘
```

### Enable Distributed Signals

```python
from uams.system import UniversalMemorySystem

ums = UniversalMemorySystem(config=UAMSConfig(
    storage_backend="redis",
    redis_enable_pubsub=True,
))

# Send cross-process signal
ums.send_signal(Signal(sender="agent_a", recipient="agent_b", signal_type="task_complete"))

# Receive signals
signals = ums.read_signals("agent_b")
```

### Resource Locks (Leases)

```python
# Acquire distributed lock
if ums.acquire_lock("agent_a", "dataset_001", ttl=300):
    try:
        # Exclusive data processing
        pass
    finally:
        ums.release_lock("agent_a", "dataset_001")
```

---

## Performance Tuning

### 1. Working Tier Cache

The WORKING tier defaults to `InMemoryStore` (hot cache) even when other tiers use Redis/Neo4j. To customize, modify `_init_stores_from_config` in `system.py`.

### 2. SQLite WAL Mode

SQLite defaults to WAL (Write-Ahead Logging), supporting concurrent reads and writes. For extremely high concurrency, switch to Redis.

### 3. Token Budget Control

```python
# Reduce retrieval budget to lower latency
results = ums.recall(query, context=ctx, budget_tokens=1000)
```

### 4. Periodic Expired Memory Cleanup

```python
import time

while True:
    time.sleep(300)
    ums.decay_sweep()  # Clean expired Working memories
```

### 5. Batch Operations

```python
# Use event bus for batch processing
for event in events:
    ums.observe(event)
```

---

## Troubleshooting

### Startup Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: redis` | redis package not installed | `pip install redis` or `pip install redis neo4j` |
| `Neo4j connection failed` | Neo4j service not running | Check `docker compose -f docker-compose.neo4j.yml up` |
| `SQLite database locked` | Concurrent write conflict | Enable WAL or switch to Redis |

### Slow Queries

| Symptom | Cause | Fix |
|---------|-------|-----|
| `recall` returns slowly | Large dataset without index | Switch to SQLite/Neo4j, ensure indexes are created |
| High CPU usage | Frequent graph traversal | Limit graph depth=1 or cache relations |
| Memory growth | Working tier not cleaned | Call `decay_sweep()` or reduce TTL |

### Data Loss

| Symptom | Cause | Fix |
|---------|-------|-----|
| Memories disappear after restart | Using memory backend | Switch to sqlite/redis/neo4j |
| Some memories lost | ForgettingEngine cleanup | Set importance > 7.0 to retain |

---

## Upgrade Guide

### Upgrading from memory to sqlite

```bash
# 1. Stop service
# 2. Export memories if needed
# 3. Set environment variables
export UAMS_STORAGE_BACKEND=sqlite
export UAMS_SQLITE_PATH=/data/uams.db
# 4. Restart service
```

### Upgrading from sqlite to redis

```bash
# 1. Start Redis service
docker compose -f docker-compose.yml -f docker-compose.redis.yml up -d redis
# 2. Switch environment variables
export UAMS_STORAGE_BACKEND=redis
export UAMS_REDIS_HOST=redis
# 3. Restart UAMS service
# 4. Manually migrate data if historical data exists
```

---

## Appendix: Production Checklist

- [ ] Persistent backend selected (sqlite/redis/neo4j)
- [ ] Log level configured (INFO or WARNING)
- [ ] Health check port enabled (3111)
- [ ] Privacy filter rules configured (if handling sensitive data)
- [ ] Periodic `decay_sweep()` or TTL configured
- [ ] SQLite/Neo4j data volumes backed up
- [ ] Fault degradation tested (disconnect Redis/Neo4j and observe graceful behavior)
