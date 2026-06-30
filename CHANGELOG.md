# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
