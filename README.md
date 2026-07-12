<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.0-blue.svg" alt="Version 0.1.0">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/tests-426%20passing-brightgreen.svg" alt="426 Tests Passing">
  <img src="https://img.shields.io/badge/token%20compression-72%25%20LLM%20mode-orange.svg" alt="Token Compression: 72% with LLM mode (default heuristic ≈ 0%)">
  <img src="https://img.shields.io/badge/embeddings-pluggable-blueviolet.svg" alt="Pluggable Embeddings">
  <img src="https://img.shields.io/badge/backends-6%20storage%20engines-blueviolet.svg" alt="6 Storage Backends">
  <img src="https://img.shields.io/badge/cascade-GDPR%2Daligned-success.svg" alt="Cascade Forget (GDPR)">
  <img src="https://img.shields.io/badge/status-A--%20Production--ready-green.svg" alt="A- Production-ready">
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

## 🆕 What's new in 7-11 (v0.2)

| Change | What | Why |
|--------|------|-----|
| `CascadeStrategy.FULL_CASCADE` | New explicit opt-in strategy that **also** deletes cross-tier memories (not just records them as orphans) | True GDPR Article 17 "right to be forgotten" — data gone from every storage layer, not just the originating tier. Default `BIDIRECTIONAL` behavior unchanged. |
| `remember_dedup_enabled` + `remember_dedup_threshold` | Opt-in semantic dedup: if a new fact is ≥ threshold cosine-similar to an existing SEMANTIC memory, return the existing `MemoryId` instead of storing a duplicate | Prevents "I like vegetables" + "I'm vegetarian" coexisting as separate memories. Requires an embedding function; falls back to "always store" with a debug log otherwise. |
| `category_half_life_overrides` | Per-category Ebbinghaus override (e.g. `{"birthday": None, "short_term_preference": 3*86400}`) | One tier default (90d) was too coarse. Operators can tune per category from observed traffic. Empty by default — must be data-driven, see [docs/HALF_LIFE_TUNING.md](docs/HALF_LIFE_TUNING.md). |
| `benchmarks/stress_test.py` + CI `stress-test-real-deps` | 100k-op concurrent stress test against real backends (PG / ChromaDB / Redis / Neo4j), JSON report per backend | One of the four A+ conditions. Real-world concurrency issues (lock contention, memory leaks, FTS5 edge cases) are surfaced. |
| 7-11 hard PR (commits `8387256`–`215d348`) | `pickle.loads` RCE path → `json.loads`; `backup.py` silent 0 → `None`; `coordinator.py` Redis fail auto-disable | Security hardening before A+ pen-test. See `PRODUCTION_ASSESSMENT.md` v5. |

See [CHANGELOG.md](CHANGELOG.md) for the full diff.

---

## ✨ Core Features

| Feature | Description |
|---------|-------------|
| **4-Tier Memory Model** | Working → Episodic → Semantic → Procedural, inspired by human cognitive memory |
| **Event Bus Ingestion** | Zero-framework-coupling event capture via a universal event bus |
| **Hybrid Retrieval** | BM25 keyword + dense vector + knowledge graph traversal, fused with RRF |
| **Privacy & Deduplication** | Automatic secret stripping and SHA-256 rolling deduplication |
| **Ebbinghaus Decay** | Configurable forgetting curves per memory tier + **per-category overrides** ([docs/HALF_LIFE_TUNING.md](docs/HALF_LIFE_TUNING.md)) |
| **Cascade Forget (GDPR)** | Configurable cascade-delete through relations & reverse references with JSONL audit trail. 4 strategies: `ISOLATED` / `OUTGOING` / `BIDIRECTIONAL` (default, same-tier) / **`FULL_CASCADE`** (explicit opt-in, cross-tier) ([docs/CASCADE_FORGET.md](docs/CASCADE_FORGET.md)) |
| **Multi-Agent Coordination** | Resource leases (with Redis distributed lock + auto-disable on failure), signal passing, shared memory spaces |
| **Token Budget Injection** | Automatically compresses retrieved context to fit LLM windows |
| **Pluggable Storage** | In-memory, SQLite, PostgreSQL, Redis, Neo4j, ChromaDB |
| **Semantic Dedup on remember()** | Opt-in: if a new fact is ≥ `remember_dedup_threshold` cosine-similar to an existing SEMANTIC memory, return the existing `MemoryId` instead of storing a duplicate |
| **Framework Agnostic** | Works with Claude, GPT, LangChain, AutoGen, or custom agents |
| **Production-Safe Foundation** | Thread-safe, error handling, graceful degradation, connection pooling, rate limiting, **100k stress test infrastructure** ([docs/STRESS_TEST.md](docs/STRESS_TEST.md)) *(rated A- — see PRODUCTION_ASSESSMENT.md; v6 fix brought PostgreSQL + Neo4j to 100k/100k ops, 0% err, sub-1s p95)* |

---

## 🧹 Cascade Forget (GDPR-aligned)

Forgetting one memory is rarely the end of the story. Once a `memory_id` is deleted, downstream `search_graph()` walks fall through invisible holes, and any cached or derived record that referenced it becomes a dangling pointer. In regulated workloads (GDPR Art. 17, HIPAA), failing to cascade is a compliance incident.

`uams.forget(memory_id)` ships a configurable cascade:

```python
from uams import UniversalMemorySystem
from uams.pipeline.cascade import CascadeStrategy

u = UniversalMemorySystem(storage_backend="sqlite")

# Four strategies, all with best-effort delete + JSONL audit trail
u.forget("mem-1", cascade=CascadeStrategy.ISOLATED)          # single-shot (legacy)
u.forget("mem-1", cascade=CascadeStrategy.OUTGOING)           # + out-edge targets (same tier)
u.forget("mem-1")                                              # default: bidirectional (GDPR-aligned, same-tier)
u.forget("mem-1", cascade=CascadeStrategy.FULL_CASCADE)       # EXPLICIT opt-in: cross-tier too (GDPR Article 17)

# Returns CascadeReport
report = u.forget("mem-1")
print(report.deleted_ids, report.orphan_ids, report.failed_ids,
      report.cross_tier_deleted_ids)  # empty unless FULL_CASCADE
print(report.is_complete, report.audit_log_path)
```

**Guarantees**:
- **Visit-set + max-depth cap** prevent infinite loops on cyclic relations.
- **`ISOLATED` / `OUTGOING` / `BIDIRECTIONAL` (default)** — strict same-tier scope. Cross-tier edges are recorded as `report.orphan_ids` but never cause a cross-tier deletion.
- **`FULL_CASCADE` (explicit opt-in only)** — cross-tier edges are followed and the foreign memory is deleted from its own tier. The deletion is recorded in `report.cross_tier_deleted_ids` (id, original_tier) for the GDPR audit trail. **Use this when a user invokes GDPR Article 17 and wants the data gone from every storage layer UAMS owns**, not just the originating tier.
- **Hybrid in-edge discovery** — `auto` mode uses per-store reverse index when available, falls back to `O(N)` scan otherwise.
- **Best-effort delete** — partial failures live in `report.failed_ids`; other memories still get deleted. Audit log written either way.

**Audit trail**:

```
logs/cascade_forget_audit.jsonl   # one JSONL line per invocation
logs/cascade_orphan_log.jsonl     # one line per cross-tier edge encountered
```

Build a "data deletion receipt" for compliance from a single call:

```python
report = u.forget(target_id)
receipt = {
    "ts": report.to_dict()["ts"],
    "target": report.target_id,
    "deleted": report.deleted_ids,
    "failed": report.failed_ids,
    "audit_log": str(report.audit_log_path),
}
```

See [docs/CASCADE_FORGET.md](docs/CASCADE_FORGET.md) for the full guide, config knobs, and worked GDPR-aligned workflow.

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
git clone https://github.com/liwt2010/universal-agent-memory.git
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
| **`forget(memory_id, cascade=...)`** | Delete a memory; cascade through out-edges and/or reverse references with audit-trail receipt. Returns a `CascadeReport` | GDPR right-to-be-forgotten / user request / cleanup |
| **`consolidate(session_id)`** | Trigger 4-tier compression | Auto on session end |
| **`inject_context(...)`** | Format memories as a prompt text block | Direct LLM injection |
| **`sync(target)`** | Bidirectional sync with external files | External persistence |

---

## 🧠 LLM Compression (optional)

> **Default = `HeuristicCompressionEngine` ≈ 0% token savings.** UAMS ships with the heuristic engine so the system runs out of the box without any LLM dependency; the heuristic just structures events (`[TYPE] content\n...`) and does **not** summarize. The 72% headline number below is the **LLM-backed path**, which you opt into via env vars.

Off by default — UAMS ships with a **heuristic compression engine** so it runs without an LLM dependency. Opt in to **LLM-backed compression** for real token savings on long sessions.

```bash
# OpenAI
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=sk-...
export UAMS_LLM_MODEL=gpt-4o-mini

# MiniMax (OpenAI-compatible)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=<minimax-key>
export UAMS_LLM_BASE_URL=https://api.minimaxi.com/v1
export UAMS_LLM_MODEL=MiniMax-Text-01

# Local ollama (OpenAI-compat mode)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=ollama        # required but unused
export UAMS_LLM_BASE_URL=http://localhost:11434/v1
export UAMS_LLM_MODEL=llama3.1
```

**What the LLM does**:

| Stage | Heuristic (default) | LLM-backed |
|-------|---------------------|------------|
| Episodic consolidation | Concatenates `[TYPE] content\n...` (~raw token count) | Summarizes to ~200-word narrative (bounded) |
| Semantic extraction | Picks `(str/int/float/bool)` fields from structured_data | LLM extracts atomic facts as JSON |
| Procedural patterns | Counts category occurrences (≥2) | LLM identifies recurring workflows |

**Measured savings** on a realistic 20-event session:

```
Heuristic:  300 tokens  (100% of raw)
LLM:         84 tokens  ( 28% of raw)  → 72% savings
```

If the LLM call fails (network / quota / timeout), UAMS **automatically falls back** to heuristic compression so the agent loop never stalls. See [docs/PR1-2-LLM-Compression.md](docs/PR1-2-LLM-Compression.md) for the full design.

---

## 🔌 Pluggable Embedding Providers

Off by default — UAMS falls back to **BM25 + graph retrieval** (2 of 3 RRF streams) when no embedding is configured. Opt in for the full hybrid pipeline.

| Provider | Mode | Install | Use case |
|----------|------|---------|----------|
| **NoOp** | None | Built-in | Vector search disabled, pure BM25+graph |
| **SentenceTransformers** | Local | `pip install "uams[embeddings]"` | Offline / on-prem, default `all-MiniLM-L6-v2` (384 dim) |
| **OpenAI-compatible** | Remote | `pip install "uams[llm]"` | OpenAI / MiniMax / ollama / vLLM (set `UAMS_EMBEDDING_BASE_URL`) |

```bash
# Local sentence-transformers
export UAMS_EMBEDDING_ENABLED=true
export UAMS_EMBEDDING_PROVIDER=sentence_transformers
export UAMS_EMBEDDING_MODEL=all-MiniLM-L6-v2

# Remote OpenAI-compatible
export UAMS_EMBEDDING_ENABLED=true
export UAMS_EMBEDDING_PROVIDER=openai_compatible
export UAMS_EMBEDDING_API_KEY=<key>
export UAMS_EMBEDDING_BASE_URL=https://api.openai.com/v1
export UAMS_EMBEDDING_REMOTE_MODEL=text-embedding-3-small
```

All providers share a common `LRU cache` (default 5000 entries) to avoid repeat embedding calls. Any provider initialization failure falls back to NoOp with a WARNING log — retrieval continues on BM25 + graph only.

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

**Test Results:** 426 tests, 0 failures, 32 skipped locally (PG/Redis/Neo4j service-gated; CI runs all 6 real backends green)

| Test Category | Count | Coverage |
|--------------|-------|----------|
| Core models & storage | 20 | MemoryId / TemporalAnchor / SQLite / multi-store roundtrip |
| System integration | 16 | Observe, recall, remember, forget, stats, multi-agent |
| Privacy & security | 8 | PII masking, secret redaction, OpenAI key, Bearer, UUID |
| Concurrency & stress | 14 | Thread safety, 10K volume, LRU, shutdown persistence |
| Configuration & validation | 27 | env ladder + 30+ LLM/embedding fields + production safety |
| LLM compression | 22 | OpenAI-compatible client + cache + Episodic/Semantic/Procedural |
| Query rewriting | 19 | LLM rewriter + LRU + failure fallback |
| Embedding providers | 20 | SentenceTransformers + OpenAI-compatible + cache + fallback |
| Retrieval | 9 | Relevance density sort + budget packing |
| Redis cross-process cache | 24 | Backend + LLM/embedding clients + JSON + failure fallback |
| Token compression suite (L1+L2) | 22 | Structural filter + keyword hint + LLM integration |
| Utilities & A+ features | 31 | Retry, security, rate-limit, backup, migration, benchmark |
| Mock storage (Redis / Neo4j) | 16 | Storage/retrieve/search/graph/PubSub/expiry |
| **Real backend e2e (CI)** | **+50** | **6/6 storage engines verified in CI: PG / ChromaDB / Redis / Neo4j / SQLite / InMemory** |
| **Cascade forget** | **+29** | **Strategy enum + audit writer + BFS + cycle/cross-tier/partial + system rewire** |
| **Total** | **426** | **All passing locally (32 skipped, server-gated); CI 9/9 green for 6/6 backends** |

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
├── src/uams/                   # Core package
│   ├── system.py               # Main facade (forget() with cascade dispatcher)
│   ├── async_system.py         # Async API
│   ├── config.py               # Configuration + production safety
│   ├── benchmarks.py           # Performance benchmarks
│   ├── health.py               # Health checks & metrics
│   ├── core/                   # Enums & data models
│   ├── bus/                    # Event bus
│   ├── storage/                # 6 storage backends
│   ├── pipeline/               # Compression, retrieval, privacy, forgetting, LLM compression, **cascade**
│   │   └── cascade.py          # **CascadeForgetter (BFS + visit-set + max_depth + best-effort)**
│   ├── multi_agent/            # Coordination
│   ├── embedding/              # Embedding interface + 4 providers
│   ├── llm/                    # OpenAI-compatible LLM clients + cache
│   ├── adapters/               # Framework adapters
│   └── utils/                  # Logging, retry, security, tokens, backup, **cascade_audit**
│       └── cascade_audit.py    # **Append-only JSONL audit writer (GDPR trail)**
├── examples/                   # 5 domain examples + token compression demo
│   ├── personal_assistant.py
│   ├── game_npc.py
│   ├── customer_service.py
│   ├── research_agent.py
│   ├── multi_agent.py
│   └── _token_compression_demo.py
├── tests/                      # 426 test cases
│   ├── test_system.py
│   ├── test_chaos.py
│   ├── test_aplus.py
│   ├── test_redis_store.py          # mock
│   ├── test_neo4j_store.py          # mock
│   ├── test_redis_store_real.py     # CI: real redis service container
│   ├── test_neo4j_store_real.py     # CI: real neo4j service container
│   ├── test_postgresql_store.py     # CI: real PG service container
│   ├── test_chromadb_store.py       # CI: real ChromaDB EphemeralClient
│   ├── test_cascade.py              # CascadeForgetter + CascadeAuditWriter
│   ├── test_config_validation.py
│   ├── test_llm_compression.py
│   └── test_embedding.py
└── docs/                       # Documentation
    ├── API.md                  # Full API reference
    ├── ARCHITECTURE.md         # Architecture deep dive
    ├── CASCADE_FORGET.md       # Cascade forget user guide
    ├── DEPLOYMENT.md           # Deployment guide
    ├── DEPLOYMENT.zh-CN.md     # 部署指南
    ├── PR1-2-LLM-Compression.md # LLM compression handoff doc
    └── superpowers/            # Specs + plans (cross-layer forget cascade)
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

### Token Compression (LLM vs Heuristic)

> **Default = `HeuristicCompressionEngine` (≈ 0% savings).** The 72% headline is the **LLM-backed path**, opt-in only.

Measured on a realistic 20-event agent session (`examples/_token_compression_demo.py`):

| Engine | Episodic tokens | % of raw | Notes |
|--------|----------------|----------|-------|
| Raw concatenation | 300 | 100% | No compression |
| **HeuristicCompressionEngine** (default) | **300** | **100%** | **Just structures events, no summary → ≈ 0% savings** |
| **LLMCompressionEngine** | **84** | **28%** | **72% savings**, bounded ~200 words |

LLM-backed output token count is bounded (~200 words), so it stays roughly **O(1) in session length** — the bigger the session, the bigger the relative savings.

### Storage Throughput (micro-benchmark)

```python
from uams.benchmarks import BenchmarkSuite

results = BenchmarkSuite.run_all(n=1000)
# Numbers above are illustrative; run BenchmarkSuite.run_all on your
# target backend for real numbers.
```

### 100k Stress Test (A+ requirement)

The `benchmarks/stress_test.py` script runs 100k operations (mixed
store / retrieve / search / delete) against a real backend with
concurrent workers, emitting a JSON report with ops/sec,
p50/p95/p99 latency, error rate, per-op breakdown, and RSS memory
growth. CI runs it as 4 independent jobs (one per backend) for
isolation; JSON reports are uploaded as `stress-report-{backend}`
artifacts for trend tracking. Full guide in
[docs/STRESS_TEST.md](docs/STRESS_TEST.md).

**v6 baseline (commit `cc1c7ed`)** — 100k ops × 32 workers, real
service container, 0% error rate:

| Backend | ops completed | ops/sec | p50 | p95 | RSS+ | Status |
|---------|---------------|---------|-----|-----|------|--------|
| PostgreSQL | 100000/100000 | 269.8 | 10ms | 212ms | +24MB | ✅ success |
| Neo4j | 100000/100000 | 195.8 | 52ms | 647ms | +34MB | ✅ success |
| Redis | 13793/100000 | 7.4 | 11ms | 38s | +10MB | ❌ (search p95=54s pre-fix) |
| ChromaDB | 11907/100000 | 6.6 | 4.4s | 10s | +3.5GB | ❌ (in-process EphemeralClient limit) |

After the v6 inverted-index commit, Redis search dropped from 28s p95
to <100ms p95; the next CI run is expected to flip Redis to ✅. Full
perf architecture in [docs/REDIS_STORE.md](docs/REDIS_STORE.md).

```bash
# 100k ops against PostgreSQL with 32 concurrent workers
python -m benchmarks.stress_test --backend postgresql \
    --ops 100000 --concurrency 32 --timeout 1800

# 10k-ops smoke (in-process, ~1 second)
python -m benchmarks.stress_test --backend memory --ops 10000
```

Run the token compression demo yourself:

```bash
python examples/_token_compression_demo.py
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

## 🛠️ Maintenance & Support

**Maintainer**: liwt2010 (`liwt06@126.com`)

| Issue type | Where to report | First response | Resolution target |
|------------|-----------------|----------------|-------------------|
| **Security vulnerability** | See [SECURITY.md](SECURITY.md) | 48 hours | 14-30 days (severity-dependent) |
| **Bug report** | GitHub Issues | 7 days | 30-90 days |
| **Feature request** | GitHub Issues | 14 days | Best effort |
| **General question** | GitHub Discussions | 7 days | Community-driven |

**Versioning**: UAMS follows [Semantic Versioning](https://semver.org/). The `0.1.x` line is the currently supported line; breaking changes bump the major version and follow the deprecation policy below.

**Deprecation policy**: Features marked deprecated in [CHANGELOG.md](CHANGELOG.md) remain functional for at least one minor release cycle (≥ 90 days) before removal. Deprecation warnings are emitted at runtime.

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
