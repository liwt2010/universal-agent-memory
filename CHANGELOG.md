# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **LLM-backed compression engine** (`LLMCompressionEngine`) inheriting `CompressionEngine`
  - Episodic summarization with two-level batching for long sessions
  - Semantic extraction via JSON-array output (tolerant of `\`\`\`json` fences)
  - Procedural pattern detection across episodes
  - Auto-fallback to `HeuristicCompressionEngine` on any LLM failure
- **OpenAI-compatible LLM client** (`OpenAICompatibleClient`) — works with OpenAI / MiniMax / ollama / vLLM via `base_url` configuration
- **`NullLLMClient`** + **`CachedLLMClient`** (in-process LRU by messages+kwargs hash)
- **Pluggable embedding providers**: `SentenceTransformersProvider` (local) and `OpenAICompatibleEmbeddingProvider` (remote) + `CachedEmbeddingProvider` (LRU)
- **Production-safety config validation** with environment strictness ladder (`development` / `staging` / `production`)
  - Rejects insecure default credentials on Neo4j / PostgreSQL / Redis in production
  - Requires TLS on credentialed backends in production
  - Bounds half-life (60s–10y), timeouts, identity-length fields
  - 30+ new `UAMSConfig` fields for LLM + embedding
- **Maintainer / response SLA**: `pyproject.toml` authors + `SECURITY.md` contact + `README.md` Maintenance & Support section (security 48h ack, bugs 7d, features 14d)
- **74 new tests** (105 → 174 total) covering config validation, LLM compression, embedding providers
- **`docs/PR1-2-LLM-Compression.md`** handoff document for the LLM compression design
- **`examples/_token_compression_demo.py`** benchmark demonstrating 72% token savings (20-event session: 300 → 84 tokens)
- PostgreSQL enterprise backend with connection pooling, JSONB, GIN indexes, and schema migrations
- Configuration validation system with 12+ constraints
- Exponential backoff retry mechanism with global statistics
- Backup and restore tools (JSONL and dict formats)
- Migration tool for cross-backend data migration
- Security enhancements: SQL injection protection, XSS prevention, input sanitization, rate limiting
- Benchmark suite for performance testing (store, retrieve, search, delete)
- 42 additional A+ grade tests covering edge cases, exception paths, and chaos scenarios
- Docker and docker-compose support for Redis, Neo4j, and PostgreSQL backends

### Fixed
- Memory leaks in MetricsCollector via circular buffer aggregation
- Infinite loop in MigrationTool when using InMemoryStore as source
- Test failures caused by sanitize_all ordering and HTML entity semicolons
- Missing `importance`/`confidence` fields in restore_from_dict test data
- Graceful degradation for Redis and Neo4j when dependencies are not installed

## [0.1.0] - 2024-XX-XX

### Added
- Initial release of UAMS (Universal Agent Memory System)
- Four-tier memory model: Working → Episodic → Semantic → Procedural
- Event bus ingestion with zero framework coupling
- Hybrid retrieval pipeline: BM25 keyword + dense vector + knowledge graph + RRF fusion
- Privacy filter with automatic secret stripping and PII masking
- Deduplication window with SHA-256 rolling hash
- Ebbinghaus-inspired forgetting engine with configurable decay curves per tier
- Multi-agent coordination: resource leases, signals, shared memory spaces
- Token budget compression for LLM context windows
- Pluggable storage backends: InMemory, SQLite, ChromaDB, Redis, Neo4j
- Framework adapters: Claude, OpenAI, LangChain, AutoGen, custom agents
- 74 unit tests with thread safety, concurrency, and stress testing
- 5 example applications: personal assistant, game NPC, customer service, research agent, multi-agent
- CI/CD pipeline with GitHub Actions
- Multi-language documentation (English, 简体中文, 繁體中文)

[Unreleased]: https://github.com/uams/universal-agent-memory/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/uams/universal-agent-memory/releases/tag/v0.1.0
