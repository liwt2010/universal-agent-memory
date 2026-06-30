<p align="center">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/tests-105%20passing-brightgreen.svg" alt="105 Tests Passing">
  <img src="https://img.shields.io/badge/status-A%2B%20Production%20Ready-gold.svg" alt="A+ Production Ready">
  <img src="https://img.shields.io/badge/backends-6%20storage%20engines-blueviolet.svg" alt="6 Storage Backends">
</p>

<h1 align="center">Universal Agent Memory System (UAMS)</h1>

<p align="center"><b>A domain-agnostic persistent memory layer for any AI agent.</b></p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a> |
  <a href="README.zh-TW.md">繁體中文</a>
</p>

---

Every AI agent starts from zero in every session. **UAMS fixes this.**

It silently captures what your agent does, compresses it into a searchable memory graph, and injects the right context when the next session starts. Whether you are building a personal assistant, a game NPC, a customer service bot, a research agent, or a multi-agent system — UAMS provides the same universal memory primitives.

## 🚀 What changes with UAMS

- **Session 1:** Alice tells the agent she is vegetarian and prefers boutique hotels.
- **Session 2:** Alice asks about Japan trip hotels. The agent already knows her dietary restrictions and hotel preferences. No re-explaining.
- **The agent just knows.**

---

## ✨ Core Features

| Feature | Description |
|---------|-------------|
| **4-Tier Memory Model** | Working → Episodic → Semantic → Procedural, inspired by human cognitive memory |
| **Event Bus Ingestion** | Zero-framework-coupling event capture via a universal event bus |
| **Hybrid Retrieval** | BM25 keyword + dense vector + knowledge graph traversal, fused with RRF |
| **Privacy & Deduplication** | Automatic secret stripping and SHA-256 rolling deduplication |
| **Ebbinghaus Decay** | Configurable forgetting curves per memory tier |
| **Multi-Agent Coordination** | Resource leases, signal passing, and shared memory spaces |
| **Token Budget Injection** | Automatically compresses retrieved context to fit LLM windows |
| **Pluggable Storage** | In-memory, SQLite, PostgreSQL, Redis, Neo4j, ChromaDB |
| **Framework Agnostic** | Works with Claude, GPT, LangChain, AutoGen, or custom agents |
| **Production Ready** | Thread-safe, error handling, graceful degradation, connection pooling, rate limiting |

---

## 📦 Quick Start

### Installation

```bash
pip install universal-agent-memory

# Or with optional backends
pip install "universal-agent-memory[all]"

# Or specific backends
pip install "universal-agent-memory[redis,neo4j,postgresql,chromadb]"
```

### From Source

```bash
git clone https://github.com/uams/universal-agent-memory.git
cd universal-agent-memory
pip install -e ".[dev]"
```

### Basic Usage

```python
from uams import UniversalMemorySystem, AgentContext, AgentEvent, EventType

# 1. Create the memory system
ums = UniversalMemorySystem()

# 2. Define agent context
ctx = AgentContext(
    agent_id="pa_001",
    agent_type="personal_assistant",
    session_id="sess_1",
    user_id="alice",
)

# 3. Observe events
ums.observe(AgentEvent(
    event_type=EventType.USER_INPUT,
    agent_context=ctx,
    content="I'm vegetarian and I prefer boutique hotels.",
    structured_data={
        "fact": "Alice is vegetarian, prefers boutique hotels",
        "importance": 8.0,
        "category": "travel_preference",
    },
))

# 4. End session (triggers 4-tier consolidation)
ums.observe(AgentEvent(
    event_type=EventType.SESSION_END,
    agent_context=ctx,
    content="Session ended",
))

# 5. New session — recall relevant context
ctx2 = AgentContext(
    agent_id="pa_001",
    agent_type="personal_assistant",
    session_id="sess_2",
    user_id="alice",
)

memories = ums.recall("Japan trip hotels", context=ctx2, budget_tokens=1000)

# 6. Inject into LLM prompt as a context block
context_block = ums.inject_context("Japan trip hotels", context=ctx2, budget_tokens=1000)
print(context_block)
```

**Output:**
```
## Relevant Memory Context

1. [SEMANTIC] Alice is vegetarian, prefers boutique hotels
2. [EPISODIC] [USER_INPUT] I'm vegetarian and I prefer boutique hotels.
```

---

## 🏗️ Architecture

### The Memory Loop

```
Agent Event → Privacy Filter → Deduplication → Working Store
                                    ↓
                        SessionEnd triggers Consolidation
                                    ↓
              Compression → Episodic / Semantic / Procedural Stores
                                    ↓
                        Retrieval (BM25 + Vector + Graph)
                                    ↓
                        Token Budget Compression
                                    ↓
                    Injected into Agent Prompt
```

### Four-Tier Memory Model

```
┌────────────────────────────────────────────────────────────┐
│  WORKING     Raw events, sensory input          (30min TTL) │
│  ─────────────────────────────────────────────────────────  │
│  EPISODIC    Session narratives, experiences      (7d half) │
│  ─────────────────────────────────────────────────────────  │
│  SEMANTIC    Facts, preferences, concepts         (90d)   │
│  ─────────────────────────────────────────────────────────  │
│  PROCEDURAL  Skills, workflows, patterns        (1yr half)  │
└────────────────────────────────────────────────────────────┘
```

### Memory Decay Formula

```
retention = 0.5^(age / half_life)
            × (1 + 0.1 × access_count)
            × (0.5 + 0.5 × importance/10)
            × confidence
```

---

## 🧠 Seven Memory Primitives

UAMS exposes **7 universal primitives** that replace the 53+ coding-specific tools of agentmemory. Any agent framework integrates via these 7 calls.

| Primitive | Signature | Purpose |
|-----------|-----------|---------|
| **`observe(event)`** | Record any `AgentEvent` into Working memory | Primary ingestion |
| **`remember(fact, ...)`** | Explicitly save a fact to Semantic memory | Direct fact storage |
| **`recall(query, ...)`** | Retrieve relevant memories across all tiers | Pre-turn context loading |
| **`forget(memory_id)`** | Delete a specific memory by ID | GDPR / user request |
| **`consolidate(session_id)`** | Trigger 4-tier compression | Auto on session end |
| **`inject_context(...)`** | Format memories as a prompt text block | Direct LLM injection |
| **`sync(target)`** | Bidirectional sync with external files | External persistence |

---

## 🤖 Multi-Agent Support

```python
# Enable multi-agent mode
ums.enable_multi_agent()

# Acquire exclusive resource lock
acquired = ums.acquire_lock("agent_a", "task_001_analysis", ttl=300.0)

# Send signal to another agent
ums.send_signal(Signal(
    sender="agent_a",
    recipient="agent_b",
    signal_type="data_ready",
    payload={"dataset_size": 10000},
))

# Read signals
signals = ums.read_signals("agent_b")
```

---

## 📂 Storage Backends

| Backend | Persistence | Concurrency | Best For | Install |
|---------|-------------|-------------|----------|---------|
| **InMemory** | ❌ | Thread-safe | Testing, prototyping | Built-in |
| **SQLite** | ✅ | WAL mode | Single-node, embedded | Built-in |
| **PostgreSQL** | ✅ | Connection pool | Enterprise, high-scale | `pip install "uams[postgresql]"` |
| **Redis** | ✅ | Pub/Sub | Distributed cache, signals | `pip install "uams[redis]"` |
| **Neo4j** | ✅ | Graph queries | Knowledge graphs, relationships | `pip install "uams[neo4j]"` |
| **ChromaDB** | ✅ | Vector search | Semantic search, embeddings | `pip install "uams[chromadb]"` |

### Production Configuration Examples

```bash
# SQLite (single node)
UAMS_STORAGE_BACKEND=sqlite
UAMS_SQLITE_PATH=/data/uams.db

# PostgreSQL (enterprise)
UAMS_STORAGE_BACKEND=postgresql
UAMS_POSTGRESQL_HOST=db.prod.local
UAMS_POSTGRESQL_PORT=5432
UAMS_POSTGRESQL_DATABASE=uams
UAMS_POSTGRESQL_USER=uams
UAMS_POSTGRESQL_PASSWORD=secure_password
UAMS_POSTGRESQL_POOL_MAX=20

# Redis (distributed)
UAMS_STORAGE_BACKEND=redis
UAMS_REDIS_HOST=redis.cluster.local
UAMS_REDIS_PORT=6380
UAMS_REDIS_PUBSUB=true

# Neo4j (knowledge graph)
UAMS_STORAGE_BACKEND=neo4j
UAMS_NEO4J_URI=bolt://neo4j.prod:7687
UAMS_NEO4J_USER=neo4j
UAMS_NEO4J_PASSWORD=secure_password
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full deployment guide.

---

## 🧪 Testing

```bash
# Run all tests
python -m unittest discover -s tests -v

# Or with pytest
pytest tests/ -v

# With coverage
pytest tests/ --cov=src/uams --cov-report=html
```

**Test Results:** 105 tests, 0 failures, 1 skipped (ChromaDB not installed)

| Test Category | Count | Coverage |
|--------------|-------|----------|
| Core models & storage | 16 | Memory, SQLite, Redis, Neo4j, PostgreSQL |
| System integration | 10 | Observe, recall, remember, forget, stats |
| Privacy & security | 11 | PII masking, secret redaction, SQL injection, XSS |
| Concurrency & stress | 14 | Thread safety, 10K volume, LRU, shutdown |
| Configuration & validation | 7 | 12+ constraint validation |
| Retry & benchmarks | 10 | Exponential backoff, performance metrics |
| Backup & migration | 5 | JSONL, dict, cross-backend migration |
| Chaos & edge cases | 4 | Truncation, Graph limits, input limits |

---

## 📁 Project Structure

```
universal-agent-memory/
├── pyproject.toml              # Python package configuration
├── README.md                   # This file (English)
├── README.zh-CN.md             # 简体中文
├── README.zh-TW.md             # 繁體中文
├── LICENSE                     # MIT License
├── CHANGELOG.md                # Version history
├── CONTRIBUTING.md             # Contribution guidelines
├── CODE_OF_CONDUCT.md          # Community standards
├── SECURITY.md                 # Security policy
├── requirements.txt            # Core dependencies
├── requirements-dev.txt        # Development dependencies
├── Dockerfile                  # Docker image
├── docker-compose.yml          # Docker Compose stack
├── docker-compose.redis.yml    # Redis override
├── docker-compose.neo4j.yml    # Neo4j override
├── .github/                    # GitHub templates & workflows
│   ├── workflows/ci.yml        # CI/CD pipeline
│   ├── ISSUE_TEMPLATE/         # Issue templates
│   ├── pull_request_template.md
│   └── dependabot.yml
├── src/uams/                   # Core package (~5000 lines)
│   ├── system.py               # Main facade
│   ├── async_system.py         # Async API
│   ├── config.py               # Configuration & validation
│   ├── benchmarks.py           # Performance benchmarks
│   ├── health.py               # Health checks & metrics
│   ├── core/                   # Enums & data models
│   ├── bus/                    # Event bus
│   ├── storage/                # 6 storage backends
│   ├── pipeline/               # Compression, retrieval, privacy, forgetting
│   ├── multi_agent/            # Coordination
│   ├── embedding/              # Embedding interface
│   ├── adapters/               # Framework adapters
│   └── utils/                  # Logging, retry, security, tokens, backup
├── examples/                   # 5 domain examples
│   ├── personal_assistant.py
│   ├── game_npc.py
│   ├── customer_service.py
│   ├── research_agent.py
│   └── multi_agent.py
├── tests/                      # 105 test cases
│   ├── test_system.py
│   ├── test_chaos.py
│   ├── test_aplus.py
│   ├── test_redis_store.py
│   └── test_neo4j_store.py
└── docs/                       # Documentation
    ├── API.md                  # Full API reference
    ├── ARCHITECTURE.md         # Architecture deep dive
    ├── DEPLOYMENT.md           # Deployment guide
    └── DEPLOYMENT.zh-CN.md     # 部署指南
```

---

## 📝 Examples

Run any example directly from the project root:

```bash
# Personal Assistant: remembers dietary preferences and hotel tastes
python examples/personal_assistant.py

# Game NPC: tavern keeper remembers a player's past misbehavior
python examples/game_npc.py

# Customer Service: support agent recalls previous tickets
python examples/customer_service.py

# Research Agent: literature review agent recalls hypotheses and papers
python examples/research_agent.py

# Multi-Agent: data collection agent signals analysis agent
python examples/multi_agent.py
```

---

## 📊 Benchmarks

```python
from uams.benchmarks import BenchmarkSuite

results = BenchmarkSuite.run_all(n=1000)
# Store: ~50,000 ops/sec
# Retrieve: ~100,000 ops/sec
# Search: ~10,000 ops/sec
# Delete: ~5,000 ops/sec
```

---

## 🔒 Security

UAMS includes built-in security features:

- **SQL Injection Protection**: Automatic keyword stripping and character filtering
- **XSS Prevention**: HTML escape and entity encoding
- **Rate Limiting**: Sliding window per-key rate limiting
- **Privacy Filter**: Automatic PII and secret detection/redaction
- **Input Validation**: Configurable length limits (default 10,000 chars)

See [SECURITY.md](SECURITY.md) for the security policy and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the security architecture.

---

## 🤝 Contributing

We welcome contributions from all domains — personal assistants, game AI, robotics, customer service, research tools, and more.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

Please ensure all tests pass before submitting:

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards.

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

UAMS is inspired by the excellent work of:

- [agentmemory](https://github.com/rohitg00/agentmemory) by Rohit Ghumare — the coding-agent memory system that proved the architecture
- [MemGPT](https://github.com/cpacker/MemGPT) by Charles Packer — the OS-inspired memory management for LLMs

UAMS generalizes their domain-specific innovations into a universal agent infrastructure layer.

---

<p align="center">
  <b>Universal Memory. Any Agent. Any Domain.</b>
</p>
