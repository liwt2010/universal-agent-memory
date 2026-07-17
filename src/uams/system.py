"""The Universal Memory System (UAMS) - production-ready main facade.

Integrates event bus, tiered storage, compression, retrieval, and multi-agent coordination.
All operations are thread-safe and include error handling with graceful degradation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from uams.llm.client import LLMClient
from uams.core.enums import EventType, MemoryType, PrivacyLevel
from uams.core.models import (
    AgentContext,
    AgentEvent,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    TemporalAnchor,
)
from uams.bus.event_bus import EventBus, EventHandler
from uams.storage.base import MemoryStore
from uams.storage.memory import InMemoryStore
from uams.pipeline.retrieval import RetrievalPipeline
from uams.pipeline.compression import CompressionEngine, HeuristicCompressionEngine
from uams.pipeline.privacy import PrivacyFilter, DeduplicationWindow
from uams.pipeline.forgetting import ForgettingEngine
from uams.multi_agent.coordinator import MultiAgentCoordinator, Signal
from uams.embedding.base import EmbeddingFn
from uams.config import UAMSConfig
from uams.utils.logging import get_logger, configure_logging
from uams.utils.security import InputValidator
from uams.utils.tokens import get_default_estimator
from uams.utils.cascade_audit import CascadeAuditWriter
from uams.pipeline.cascade import (
    CascadeForgetter,
    CascadeReport,
    CascadeStrategy,
)

logger = get_logger(__name__)


@dataclass
class ConsolidateResult:
    """Outcome of a single ``consolidate()`` invocation.

    Replaces the prior ``-> None`` return so callers (and tests)
    can verify what happened without peeking at private session
    state. ``error`` is populated when consolidation partially
    failed (one tier failed but others succeeded) — a non-empty
    ``error`` does NOT mean the whole call failed; check the
    ``episodic_memory_id`` / ``semantic_facts`` / ``procedural_patterns``
    fields for actual outcomes.
    """
    session_id: str
    source_event_count: int = 0
    episodic_memory_id: str | None = None
    semantic_facts: int = 0
    procedural_patterns: int = 0
    duration_ms: float = 0.0
    error: str | None = None


class UniversalMemorySystem(EventHandler):
    """
    The main facade. Any agent framework integrates via this class.

    Thread-safe. Error handling with graceful degradation.
    Configurable via UAMSConfig.
    """

    def __init__(
        self,
        stores: dict[MemoryType, MemoryStore] | None = None,
        compression: CompressionEngine | None = None,
        embedding_fn: EmbeddingFn = None,
        privacy_filter: PrivacyFilter | None = None,
        dedup_window: DeduplicationWindow | None = None,
        config: UAMSConfig | None = None,
    ):
        self._config = config or UAMSConfig.from_env()
        self._config.validate()
        configure_logging(self._config.log_level, self._config.structured_logging)

        logger.info(
            "UAMS initializing | backend=%s log_level=%s token_budget=%d",
            self._config.storage_backend,
            self._config.log_level,
            self._config.default_token_budget,
        )

        # Event bus
        self._bus = EventBus(max_buffer_size=self._config.event_bus_max_buffer)

        # Tiered stores (production: swap for real DBs)
        self._stores = stores or self._init_stores_from_config()

        # Processing pipeline
        self._privacy = privacy_filter or PrivacyFilter(self._config.privacy_patterns)
        self._dedup = dedup_window or DeduplicationWindow(
            window_seconds=self._config.dedup_window_seconds
        )
        self._compression = compression
        if self._compression is None:
            self._compression = self._build_compression_engine()

        self._retrieval = RetrievalPipeline(
            self._stores,
            rrf_k=self._config.rrf_k,
            token_estimator=get_default_estimator(),
            query_rewriter=self._build_query_rewriter(),
            max_results_per_session=self._config.max_results_per_session,
        )
        self._forgetting = ForgettingEngine(self._stores)
        self._cascade_audit = CascadeAuditWriter(
            path=self._config.cascade_audit_log_path,
            orphan_path=self._config.cascade_orphan_log_path,
        )
        self._cascade_forgetter = CascadeForgetter(
            stores=self._stores,
            config=self._config,
            audit_writer=self._cascade_audit,
        )
        self._coordinator: MultiAgentCoordinator | None = None

        # Embedding callable: explicit kwarg wins; otherwise build from config.
        if embedding_fn is not None:
            self._embedding_fn = embedding_fn
        else:
            self._embedding_fn = self._build_embedding_fn()

        # Register ourselves on the event bus for consolidation triggers
        self._bus.subscribe(self, [EventType.SESSION_END, EventType.SUBSESSION_END])

        # Session tracking: session_id -> list of events
        self._session_events: dict[str, list[AgentEvent]] = {}
        self._session_lock = threading.RLock()
        # Process-wide lock for decay_sweep: prevents two concurrent sweeps
        # (e.g. a slow SQLite sweep that runs >60s colliding with the next
        # tick of the docker-entrypoint loop, or two callers triggering
        # sweeps from different threads) from racing through the store
        # delete_expired() implementations.
        self._sweep_lock = threading.Lock()

    def _build_compression_engine(self):
        """Construct the compression engine.

        Uses ``LLMCompressionEngine`` when ``llm_enabled=True`` and an API key
        is configured. Falls back to ``HeuristicCompressionEngine`` on any
        initialization failure so the agent loop never stalls.
        """
        if not (self._config.llm_enabled and self._config.llm_api_key):
            return HeuristicCompressionEngine()

        try:
            from uams.llm.client import OpenAICompatibleClient
            from uams.pipeline.llm_compression import LLMCompressionEngine

            inner = OpenAICompatibleClient(
                api_key=self._config.llm_api_key,
                base_url=self._config.llm_base_url,
                model=self._config.llm_model,
                timeout=self._config.llm_timeout_seconds,
                max_retries=self._config.llm_max_retries,
            )
            client = self._wrap_with_cache(
                inner,
                in_process_max_entries=self._config.llm_cache_max_entries,
                in_process_cache_enabled=self._config.llm_cache_enabled,
            )
            engine = LLMCompressionEngine(
                client,
                max_events_per_call=self._config.llm_compression_max_events,
                target_ratio=self._config.llm_compression_target_ratio,
                timeout=self._config.llm_timeout_seconds,
                max_tokens=self._config.llm_max_tokens,
                temperature=self._config.llm_temperature,
            )
            logger.info(
                "LLM compression engine enabled | provider=%s model=%s base_url=%s",
                self._config.llm_provider,
                self._config.llm_model,
                self._config.llm_base_url,
            )
            return engine
        except Exception:
            logger.exception(
                "Failed to initialize LLMCompressionEngine; falling back to HeuristicCompressionEngine"
            )
            return HeuristicCompressionEngine()

    def _build_embedding_fn(self):
        """Build the embedding callable from config.

        Returns ``None`` when no embedding is configured, which causes the
        system to fall back to BM25 + graph retrieval only.
        """
        if not self._config.embedding_enabled:
            return None
        if self._config.embedding_provider == "noop":
            return None
        try:
            provider = self._build_embedding_provider()
        except Exception:
            logger.exception(
                "Failed to initialize embedding provider '%s'; "
                "falling back to noop embedding (vector search disabled)",
                self._config.embedding_provider,
            )
            return None
        if self._config.embedding_cache_enabled:
            from uams.embedding.client import CachedEmbeddingProvider
            provider = CachedEmbeddingProvider(
                provider, max_entries=self._config.embedding_cache_max_entries
            )
        logger.info(
            "Embedding provider enabled | provider=%s model=%s dimension=%d",
            self._config.embedding_provider,
            getattr(provider, "model_name", "?"),
            self._config.embedding_dimension,
        )
        return provider.embed

    def _build_embedding_provider(self):
        """Construct the configured embedding provider."""
        if self._config.embedding_provider == "sentence_transformers":
            from uams.embedding.client import SentenceTransformersProvider
            return SentenceTransformersProvider(
                model_name=self._config.embedding_model,
                device=self._config.embedding_device,
                batch_size=self._config.embedding_batch_size,
            )
        if self._config.embedding_provider == "openai_compatible":
            from uams.embedding.client import OpenAICompatibleEmbeddingProvider
            return OpenAICompatibleEmbeddingProvider(
                api_key=self._config.embedding_api_key,
                base_url=self._config.embedding_base_url,
                model=self._config.embedding_remote_model,
                timeout=self._config.embedding_timeout_seconds,
                max_retries=self._config.embedding_max_retries,
            )
        # Should be unreachable given validate(); be defensive
        raise ValueError(
            f"Unknown embedding_provider: {self._config.embedding_provider}"
        )

    def _build_query_rewriter(self):
        """Build the optional ``QueryRewriter`` from config.

        Returns ``None`` when query rewriting is disabled or when no LLM
        client can be constructed. Rewriter shares the same LLM client
        configuration (api_key, base_url, model) as the compression engine.
        """
        if not self._config.query_rewrite_enabled:
            return None
        if not (self._config.llm_enabled and self._config.llm_api_key):
            return None
        try:
            from uams.llm.client import OpenAICompatibleClient
            from uams.pipeline.query_rewrite import QueryRewriter

            inner = OpenAICompatibleClient(
                api_key=self._config.llm_api_key,
                base_url=self._config.llm_base_url,
                model=self._config.llm_model,
                timeout=self._config.query_rewrite_timeout_seconds,
                max_retries=self._config.llm_max_retries,
            )
            client = self._wrap_with_cache(
                inner,
                in_process_max_entries=self._config.query_rewrite_cache_max_entries,
                in_process_cache_enabled=self._config.query_rewrite_cache_enabled,
            )
            return QueryRewriter(
                llm_client=client,
                max_variants=self._config.query_rewrite_max_variants,
                timeout=self._config.query_rewrite_timeout_seconds,
                cache_max_entries=self._config.query_rewrite_cache_max_entries,
                max_tokens=self._config.llm_max_tokens,
                temperature=self._config.llm_temperature,
            )
        except Exception:
            logger.exception(
                "Failed to initialize QueryRewriter; query rewriting disabled for this session"
            )
            return None

    def _build_cache_backend(self):
        """Build the configured cache backend (memory or Redis).

        Returns ``None`` for ``memory`` (cached clients use their in-process
        LRU). Returns a ``RedisCacheBackend`` for ``redis`` (cross-process
        sharing). Silently falls back to ``None`` if Redis is unavailable.
        """
        if self._config.cache_backend != "redis":
            return None
        try:
            from uams.cache.redis_backend import RedisCacheBackend
            return RedisCacheBackend(
                host=self._config.redis_cache_host,
                port=self._config.redis_cache_port,
                db=self._config.redis_cache_db,
                password=self._config.redis_cache_password,
                ttl_seconds=self._config.redis_cache_ttl_seconds,
                key_prefix=self._config.redis_cache_key_prefix,
            )
        except Exception:
            logger.exception(
                "Failed to initialize RedisCacheBackend; falling back to in-process LRU"
            )
            return None

    def _wrap_with_cache(
        self,
        inner: "LLMClient",
        in_process_max_entries: int,
        in_process_cache_enabled: bool,
    ) -> "LLMClient":
        """Wrap an LLM client with cache (Redis if configured, else in-process LRU)."""
        redis_backend = self._build_cache_backend()
        if redis_backend is not None:
            from uams.llm.client import CachedLLMClient
            return CachedLLMClient(
                inner,
                max_entries=in_process_max_entries,
                cache_get=redis_backend.cache_get_callable(),
                cache_put=redis_backend.cache_put_callable(),
            )
        if in_process_cache_enabled:
            from uams.llm.client import CachedLLMClient
            return CachedLLMClient(inner, max_entries=in_process_max_entries)
        return inner

    def _init_stores_from_config(self) -> dict[MemoryType, MemoryStore]:
        """Initialize storage backends based on configuration."""
        backend = self._config.storage_backend
        max_cap = self._config.memory_capacity
        if backend == "memory":
            return {
                MemoryType.WORKING: InMemoryStore(max_capacity=max_cap),
                MemoryType.EPISODIC: InMemoryStore(max_capacity=max_cap),
                MemoryType.SEMANTIC: InMemoryStore(max_capacity=max_cap),
                MemoryType.PROCEDURAL: InMemoryStore(max_capacity=max_cap),
            }
        elif backend == "sqlite":
            try:
                from uams.storage.sqlite import SQLiteStore
                return {
                    MemoryType.WORKING: InMemoryStore(max_capacity=max_cap),  # Working stays hot in-memory
                    MemoryType.EPISODIC: SQLiteStore(
                        self._config.sqlite_path, "episodic",
                        pool_size=self._config.sqlite_pool_size,
                    ),
                    MemoryType.SEMANTIC: SQLiteStore(
                        self._config.sqlite_path, "semantic",
                        pool_size=self._config.sqlite_pool_size,
                    ),
                    MemoryType.PROCEDURAL: SQLiteStore(
                        self._config.sqlite_path, "procedural",
                        pool_size=self._config.sqlite_pool_size,
                    ),
                }
            except Exception:
                logger.exception("Failed to initialize SQLiteStore, falling back to InMemoryStore")
                return {
                    MemoryType.WORKING: InMemoryStore(max_capacity=max_cap),
                    MemoryType.EPISODIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.SEMANTIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.PROCEDURAL: InMemoryStore(max_capacity=max_cap),
                }
        elif backend == "redis":
            try:
                from uams.storage.redis import RedisStore
                redis = RedisStore(
                    host=self._config.redis_host,
                    port=self._config.redis_port,
                    db=self._config.redis_db,
                    password=self._config.redis_password,
                    key_prefix=self._config.redis_key_prefix,
                    ttl_seconds=self._config.redis_ttl_seconds,
                    enable_pubsub=self._config.redis_enable_pubsub,
                )
                if not redis._available:
                    raise RuntimeError("Redis connection failed")
                logger.info("Redis backend initialized for all tiers")
                return {
                    MemoryType.WORKING: redis,
                    MemoryType.EPISODIC: redis,
                    MemoryType.SEMANTIC: redis,
                    MemoryType.PROCEDURAL: redis,
                }
            except Exception:
                logger.exception("Failed to initialize RedisStore, falling back to InMemoryStore")
                return {
                    MemoryType.WORKING: InMemoryStore(max_capacity=max_cap),
                    MemoryType.EPISODIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.SEMANTIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.PROCEDURAL: InMemoryStore(max_capacity=max_cap),
                }
        elif backend == "neo4j":
            try:
                from uams.storage.neo4j import Neo4jStore
                neo4j = Neo4jStore(
                    uri=self._config.neo4j_uri,
                    user=self._config.neo4j_user,
                    password=self._config.neo4j_password,
                    database=self._config.neo4j_database,
                    ttl_seconds=self._config.neo4j_ttl_seconds,
                )
                if not neo4j._available:
                    raise RuntimeError("Neo4j connection failed")
                logger.info("Neo4j backend initialized for all tiers")
                return {
                    MemoryType.WORKING: neo4j,
                    MemoryType.EPISODIC: neo4j,
                    MemoryType.SEMANTIC: neo4j,
                    MemoryType.PROCEDURAL: neo4j,
                }
            except Exception:
                logger.exception("Failed to initialize Neo4jStore, falling back to InMemoryStore")
                return {
                    MemoryType.WORKING: InMemoryStore(max_capacity=max_cap),
                    MemoryType.EPISODIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.SEMANTIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.PROCEDURAL: InMemoryStore(max_capacity=max_cap),
                }
        elif backend == "postgresql":
            try:
                from uams.storage.postgresql import PostgreSQLStore
                pg = PostgreSQLStore(
                    host=self._config.postgresql_host,
                    port=self._config.postgresql_port,
                    database=self._config.postgresql_database,
                    user=self._config.postgresql_user,
                    password=self._config.postgresql_password,
                    table_name=self._config.postgresql_table,
                    pool_min=self._config.postgresql_pool_min,
                    pool_max=self._config.postgresql_pool_max,
                )
                if not pg._available:
                    raise RuntimeError("PostgreSQL connection failed")
                logger.info("PostgreSQL backend initialized for all tiers")
                return {
                    MemoryType.WORKING: pg,
                    MemoryType.EPISODIC: pg,
                    MemoryType.SEMANTIC: pg,
                    MemoryType.PROCEDURAL: pg,
                }
            except Exception:
                logger.exception("Failed to initialize PostgreSQLStore, falling back to InMemoryStore")
                return {
                    MemoryType.WORKING: InMemoryStore(max_capacity=max_cap),
                    MemoryType.EPISODIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.SEMANTIC: InMemoryStore(max_capacity=max_cap),
                    MemoryType.PROCEDURAL: InMemoryStore(max_capacity=max_cap),
                }
        else:
            logger.warning("Unknown storage backend '%s', defaulting to memory", backend)
            return {
                MemoryType.WORKING: InMemoryStore(max_capacity=max_cap),
                MemoryType.EPISODIC: InMemoryStore(max_capacity=max_cap),
                MemoryType.SEMANTIC: InMemoryStore(max_capacity=max_cap),
                MemoryType.PROCEDURAL: InMemoryStore(max_capacity=max_cap),
            }

    # --- EventBus handler ---

    def handle(self, event: AgentEvent) -> None:
        """Called by EventBus when SESSION_END or SUBSESSION_END occurs."""
        try:
            if event.event_type in (EventType.SESSION_END, EventType.SUBSESSION_END):
                self._consolidate_session(event.agent_context.session_id)
        except Exception:
            logger.exception(
                "Consolidation failed for session %s. Session events retained for retry.",
                event.agent_context.session_id,
            )

    # --- Public API: 7 Memory Primitives ---

    def observe(self, event: AgentEvent) -> None:
        """
        Record any event into working memory.
        This is the primary ingestion primitive.
        Error handling: if any step fails, event is still published to bus.
        """
        # 0. Enforce max_agent_id_length / max_user_id_length caps from
        # config. Truncate + warn rather than raise so a too-long ID
        # does not block ingestion; downstream filters (delete_by_filter
        # etc.) are keyed on these strings and would silently miss on
        # mismatch.
        ctx = event.agent_context
        if ctx.agent_id and len(ctx.agent_id) > self._config.max_agent_id_length:
            logger.warning(
                "agent_id length %d exceeds max_agent_id_length=%d; truncating",
                len(ctx.agent_id), self._config.max_agent_id_length,
            )
            ctx.agent_id = ctx.agent_id[: self._config.max_agent_id_length]
        if ctx.user_id and len(ctx.user_id) > self._config.max_user_id_length:
            logger.warning(
                "user_id length %d exceeds max_user_id_length=%d; truncating",
                len(ctx.user_id), self._config.max_user_id_length,
            )
            ctx.user_id = ctx.user_id[: self._config.max_user_id_length]

        # 1. Publish to event bus (notifies all subscribers)
        try:
            self._bus.publish(event)
        except Exception:
            logger.exception("EventBus publish failed for event %s. Continuing.", event.event_id)

        # 2. Track for session consolidation (with lock). Enforce
        # ``max_session_events`` cap so a runaway event source can't
        # blow up memory: oldest events are dropped on overflow.
        try:
            with self._session_lock:
                sid = event.agent_context.session_id
                if sid not in self._session_events:
                    self._session_events[sid] = []
                events = self._session_events[sid]
                events.append(event)
                cap = self._config.max_session_events
                if cap > 0 and len(events) > cap:
                    dropped = len(events) - cap
                    del events[:dropped]
                    logger.warning(
                        "Session %s exceeded max_session_events cap (%d); "
                        "dropped %d oldest events",
                        sid, cap, dropped,
                    )
        except Exception:
            logger.exception("Session tracking failed for %s", event.agent_context.session_id)
            return  # Can't proceed without session tracking

        # 3. Create working memory from event (with length protection)
        try:
            sanitized_raw = self._privacy.sanitize(event.content, event.privacy)
        except Exception:
            logger.exception("Privacy sanitization failed. Using raw content.")
            sanitized_raw = event.content
        
        truncated_raw = self._truncate_raw(sanitized_raw)
        safe_raw = InputValidator.sanitize_all(truncated_raw, max_length=self._config.max_raw_length)

        mem = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(
                created_at=event.timestamp,
                expires_at=event.timestamp + self._config.working_ttl_seconds,
            ),
            context=event.agent_context,
            payload=MemoryPayload(
                raw=safe_raw,
                structured=event.structured_data,
            ),
            metadata=MemoryMetadata(
                memory_type=MemoryType.WORKING,
                privacy=event.privacy,
                source_event=event.event_type,
                tags={event.event_type.name, event.agent_context.agent_type},
            ),
        )

        # 4. Deduplication
        try:
            if not self._dedup.is_duplicate(mem.payload):
                self._stores[MemoryType.WORKING].store(mem)
                logger.debug(
                    "Stored working memory %s for session %s",
                    mem.id, event.agent_context.session_id
                )
        except Exception:
            logger.exception("Failed to store working memory. Event may be lost.")

    def remember(
        self,
        fact: str,
        context: AgentContext,
        importance: float = 5.0,
        category: str = "general",
        privacy: PrivacyLevel = PrivacyLevel.PUBLIC,
        tags: set | None = None,
    ) -> MemoryId | None:
        """
        Explicitly save a fact/preference/pattern to semantic memory.
        Returns MemoryId on success, None on failure (graceful degradation).

        When ``UAMSConfig.remember_dedup_enabled`` is True and an
        embedding function is available, the SEMANTIC store is searched
        first; if an existing memory has cosine similarity >=
        ``remember_dedup_threshold`` to the new fact, that existing
        MemoryId is returned and the new fact is NOT stored. This
        prevents semantic noise like storing both "I like vegetables"
        and "I'm vegetarian" as separate memories. Dedup is opt-in
        because it requires the embedding function; without it,
        remember() always stores.
        """
        try:
            truncated_fact = self._truncate_raw(fact)
            safe_fact = InputValidator.sanitize_all(truncated_fact, max_length=self._config.max_raw_length)

            # --- Embedding (also reused for dedup) ---
            embedding: list[float] | None = None
            if self._embedding_fn:
                try:
                    embedding = self._embedding_fn(truncated_fact)
                except Exception:
                    logger.exception("Embedding failed for fact '%s...'. Storing without embedding.", truncated_fact[:50])
                    embedding = None

            # --- Optional dedup (opt-in, requires embedding) ---
            if self._config.remember_dedup_enabled:
                if embedding is None:
                    logger.debug(
                        "remember_dedup_enabled=True but no embedding available; "
                        "storing new fact without dedup check."
                    )
                else:
                    existing, sim = self._find_dedup_match(
                        embedding, self._config.remember_dedup_threshold,
                    )
                    if existing is not None:
                        logger.info(
                            "remember() dedup hit: returning existing memory %s "
                            "(new fact '%s...' is %.3f similar, threshold=%.2f)",
                            existing.id, safe_fact[:50], sim,
                            self._config.remember_dedup_threshold,
                        )
                        return existing.id

            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(),
                context=context,
                payload=MemoryPayload(
                    raw=safe_fact,
                    structured={"explicit": True, "category": category},
                    embedding=embedding,
                ),
                metadata=MemoryMetadata(
                    memory_type=MemoryType.SEMANTIC,
                    privacy=privacy,
                    importance=importance,
                    categories={category} | (tags or set()),
                ),
            )

            self._stores[MemoryType.SEMANTIC].store(mem)
            logger.info(
                "Remembered fact for agent=%s session=%s category=%s",
                context.agent_id, context.session_id, category
            )
            return mem.id
        except Exception:
            logger.exception("remember() failed. Fact not stored.")
            return None

    def _find_dedup_match(
        self,
        embedding: list[float],
        threshold: float,
    ) -> tuple[Memory | None, float]:
        """Search SEMANTIC store for an existing memory with cosine sim
        >= threshold to ``embedding``.

        Returns ``(memory, similarity)`` for the best match above the
        threshold, or ``(None, 0.0)`` if nothing qualifies. Failures
        are caught and logged so dedup never breaks a remember() call.
        """
        sem_store = self._stores.get(MemoryType.SEMANTIC)
        if sem_store is None:
            return None, 0.0
        try:
            candidates = sem_store.search_vector(embedding, k=5)
        except Exception:
            logger.exception("Dedup search_vector failed; treating as no-match")
            return None, 0.0
        best: Memory | None = None
        best_sim = 0.0
        for mem in candidates:
            existing_emb = mem.payload.embedding
            if not existing_emb or len(existing_emb) != len(embedding):
                continue
            sim = self._cosine_similarity(embedding, existing_emb)
            if sim > best_sim:
                best_sim = sim
                best = mem
        if best is not None and best_sim >= threshold:
            return best, best_sim
        return None, 0.0

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Plain-Python cosine similarity (no numpy dependency)."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / ((na ** 0.5) * (nb ** 0.5))

    def recall(
        self,
        query: str,
        context: AgentContext,
        budget_tokens: int = None,
        include_working: bool = True,
    ) -> list[Memory]:
        """
        Retrieve relevant memories for injection into agent context.
        Never raises. Returns empty list on any failure.
        """
        budget_tokens = budget_tokens or self._config.default_token_budget
        try:
            vector = None
            if self._embedding_fn:
                try:
                    vector = self._embedding_fn(query)
                except Exception:
                    logger.exception("Embedding failed for query '%s...'. Falling back to keyword search.", query[:50])

            results = self._retrieval.retrieve(
                query,
                context,
                vector=vector,
                budget_tokens=budget_tokens,
            )
            logger.debug(
                "recall() returned %d memories for query='%s...' budget=%d",
                len(results), query[:50], budget_tokens
            )
            return results
        except Exception:
            logger.exception("recall() failed. Returning empty list.")
            return []

    def forget(
        self,
        memory_id: str,
        *,
        cascade: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
        max_depth: int | None = None,
        in_edge_mode: str | None = None,
    ) -> CascadeReport:
        """Forget a memory with configurable cascade.

        Strategy choices:
          - 'isolated'      : delete only `memory_id` (legacy single-shot)
          - 'outgoing'      : + delete out-edge targets (same tier)
          - 'bidirectional' : + delete reverse references too
                              (default; GDPR-aligned)

        Cross-tier edges are recorded as orphans (never deleted).

        Returns a `CascadeReport` describing what was deleted,
        what was marked orphan, and any partial failures. Never
        raises out of cascade.
        """
        # Accept both enums and plain strings.
        strategy = cascade if isinstance(cascade, CascadeStrategy) else CascadeStrategy(cascade)
        return self._cascade_forgetter.forget(
            memory_id,
            strategy=strategy,
            max_depth=max_depth,
            in_edge_mode=in_edge_mode,
        )

    def revoke_agent(
        self,
        agent_id: str,
        cascade: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
    ) -> int:
        """Delete all memories whose ``context.agent_id`` matches.

        Cross-tier delete via each tier's ``delete_by_filter()``. This
        is a per-tier implementation that hits indexed columns (SQLite
        / PG) or per-row metadata filter (Redis / ChromaDB / Neo4j).

        ``cascade`` is accepted for symmetry with ``forget()`` but is
        not propagated to per-tier deletes (those are not
        CascadeForgetter-aware). Pass it for forward compatibility
        once cross-tier graph cascades land.

        Returns total count deleted across all tiers.
        """
        return self._revoke_by_metadata("agent_id", agent_id, cascade)

    def revoke_project(
        self,
        project_id: str,
        cascade: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
    ) -> int:
        """Delete all memories whose ``context.project_id`` matches.

        Symmetric counterpart to ``revoke_agent``. Returns total count
        deleted across all tiers.
        """
        return self._revoke_by_metadata("project_id", project_id, cascade)

    def delete_by_project_id(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> int:
        """Delete all memories whose ``context.project_id`` matches.

        If ``tenant_id`` is given, only memories matching BOTH
        ``project_id`` AND ``tenant_id`` are deleted; otherwise the
        filter is ``project_id`` alone. Returns the count deleted
        (0 if no matches).

        Per-tier implementation:
          - Without ``tenant_id``: a single
            ``MemoryStore.delete_by_filter("project_id", project_id)``
            call per tier. O(matches) on every backend.
          - With ``tenant_id``: a two-pass approach is required
            because the storage layer's ``delete_by_filter`` only
            takes one equality predicate. We first list the (already
            small) ``project_id`` survivors, then delete those whose
            ``tenant_id`` matches. The list_all cap of 10000 is the
            upper bound on the survivors we will inspect per call —
            callers with more than 10000 matches under a single
            project should pass ``tenant_id=None`` (single equality)
            or accept a partial count.
        """
        deleted = 0
        if tenant_id is None:
            for store in self._stores.values():
                try:
                    deleted += store.delete_by_filter("project_id", project_id)
                except Exception:
                    logger.exception(
                        "delete_by_project_id: store failed on project_id=%r",
                        project_id,
                    )
            return deleted

        # tenant_id is set — narrow to (project_id, tenant_id) intersection
        # via a single multi-predicate query per store. v0.6.0 closes the
        # P0-1 GDPR hole where the previous implementation loaded every
        # project_id row into memory and then filtered — which silently
        # dropped anything past the 999-row list_all cap.
        for store in self._stores.values():
            try:
                deleted += store.delete_by_filters(
                    (("project_id", project_id), ("tenant_id", tenant_id))
                )
            except Exception:
                logger.exception(
                    "delete_by_project_id: store failed on "
                    "project_id=%r tenant_id=%r",
                    project_id, tenant_id,
                )
        return deleted

    def _revoke_by_metadata(
        self,
        field: str,
        value: str,
        cascade: CascadeStrategy | str = CascadeStrategy.BIDIRECTIONAL,
    ) -> int:
        """Shared helper for revoke_agent / revoke_project.

        Walks every tier store and invokes ``delete_by_filter``. Per-
        row failures are logged and skipped; the total returned
        reflects actual deletions.
        """
        # Resolve cascade for future cross-tier graph cascade wiring.
        strategy = (
            cascade if isinstance(cascade, CascadeStrategy)
            else CascadeStrategy(cascade)
        )
        logger.info(
            "Revoking all memories where %s = %r (strategy=%s)",
            field, value, strategy.value,
        )
        total = 0
        for tier, store in self._stores.items():
            try:
                n = store.delete_by_filter(field, value)
                total += n
                logger.debug(
                    "Revoke %s=%r: tier %s deleted %d",
                    field, value, tier.name, n,
                )
            except Exception:
                logger.exception(
                    "Revoke %s=%r: tier %s failed",
                    field, value, tier.name,
                )
        logger.info(
            "Revoke %s=%r complete: total=%d across %d tiers",
            field, value, total, len(self._stores),
        )
        return total

    def consolidate(self, session_id: str | None = None) -> ConsolidateResult:
        """Manually trigger 4-tier memory consolidation.

        Returns a ``ConsolidateResult`` describing the outcome — never raises.
        If ``session_id`` is None, consolidates every pending session and
        returns the result of the LAST one (with all session durations
        summed into ``duration_ms`` and ``error`` joined by newlines).
        Callers wanting per-session results should call with explicit
        ``session_id`` arguments.
        """
        start = time.monotonic()
        try:
            if session_id:
                return self._consolidate_session(session_id)
            with self._session_lock:
                sids = list(self._session_events.keys())
            if not sids:
                return ConsolidateResult(
                    session_id="<none>",
                    duration_ms=(time.monotonic() - start) * 1000.0,
                )
            results: list[ConsolidateResult] = []
            for sid in sids:
                results.append(self._consolidate_session(sid))
            # Aggregate. The last result wins for "headline" fields;
            # duration is summed, errors joined.
            last = results[-1]
            joined_errors = "\n".join(
                r.error for r in results if r.error
            ) or None
            return ConsolidateResult(
                session_id=last.session_id,
                source_event_count=sum(r.source_event_count for r in results),
                episodic_memory_id=last.episodic_memory_id,
                semantic_facts=sum(r.semantic_facts for r in results),
                procedural_patterns=sum(r.procedural_patterns for r in results),
                duration_ms=sum(r.duration_ms for r in results),
                error=joined_errors,
            )
        except Exception as exc:
            logger.exception("consolidate() failed")
            return ConsolidateResult(
                session_id=session_id or "<unknown>",
                duration_ms=(time.monotonic() - start) * 1000.0,
                error=str(exc),
            )

    def get_session_summary(self, session_id: str) -> Memory | None:
        """Get the episodic summary of a completed session."""
        try:
            results = self._stores[MemoryType.EPISODIC].search_keywords(session_id, k=1)
            return results[0] if results else None
        except Exception:
            logger.exception("get_session_summary() failed")
            return None

    def inject_context(
        self,
        query: str,
        context: AgentContext,
        budget_tokens: int = None,
    ) -> str:
        """
        Convenience: retrieve memories and format them as a text block
        ready to be injected into an LLM prompt.
        """
        budget_tokens = budget_tokens or self._config.default_token_budget
        try:
            memories = self.recall(query, context, budget_tokens=budget_tokens)
            if not memories:
                return ""

            parts = ["## Relevant Memory Context\n"]
            for i, mem in enumerate(memories, 1):
                parts.append(f"{i}. [{mem.metadata.memory_type.name}] {mem.payload.raw}")
            return "\n".join(parts)
        except Exception:
            logger.exception("inject_context() failed. Returning empty string.")
            return ""

    def decay_sweep(self) -> int:
        """Run forgetting sweep. Returns count of deleted memories.

        Process-wide lock: a second concurrent call returns 0 immediately
        (the sweep already in progress will account for the new tick's
        scope on its next run). The 0-return signal lets callers
        distinguish "sweep skipped because one is running" from
        "sweep ran and evicted 0 memories".
        """
        if not self._sweep_lock.acquire(blocking=False):
            logger.debug("decay_sweep() skipped: another sweep is in progress")
            return 0
        try:
            count = self._forgetting.sweep()
            logger.info("decay_sweep() evicted %d memories", count)
            return count
        except Exception:
            logger.exception("decay_sweep() failed")
            return 0
        finally:
            self._sweep_lock.release()

    # --- Multi-agent primitives ---

    def enable_multi_agent(self, shared_store: MemoryStore | None = None) -> None:
        """Enable multi-agent coordination with a shared memory space."""
        try:
            if shared_store is None:
                shared_store = InMemoryStore(max_capacity=self._config.memory_capacity)
            # Pass Redis client if available for distributed locks
            redis_client = None
            if self._config.storage_backend == "redis" and self._stores[MemoryType.WORKING]._available:
                redis_client = self._stores[MemoryType.WORKING]
            self._coordinator = MultiAgentCoordinator(shared_store, redis_client=redis_client)
            logger.info("Multi-agent mode enabled (distributed_locks=%s)", redis_client is not None)
        except Exception:
            logger.exception("enable_multi_agent() failed")

    def acquire_lock(self, agent_id: str, resource: str, ttl: float = 300.0) -> bool:
        """Acquire an exclusive lease on a resource. Returns True if acquired."""
        try:
            if not self._coordinator:
                logger.warning("acquire_lock called but multi-agent mode not enabled")
                return False
            lease = self._coordinator.acquire_lease(agent_id, resource, ttl)
            return lease is not None
        except Exception:
            logger.exception("acquire_lock() failed")
            return False

    def release_lock(self, agent_id: str, resource: str) -> bool:
        """Release a lease."""
        try:
            if not self._coordinator:
                return False
            return self._coordinator.release_lease(agent_id, resource)
        except Exception:
            logger.exception("release_lock() failed")
            return False

    def send_signal(self, signal: Signal) -> None:
        """Send an inter-agent signal."""
        try:
            if not self._coordinator:
                logger.warning("send_signal called but multi-agent mode not enabled")
                return
            self._coordinator.send_signal(signal)
        except Exception:
            logger.exception("send_signal() failed")

    def read_signals(self, agent_id: str) -> list[Signal]:
        """Read all unread signals for this agent."""
        try:
            if not self._coordinator:
                return []
            return self._coordinator.read_signals(agent_id)
        except Exception:
            logger.exception("read_signals() failed")
            return []

    def share_memory(self, memory: Memory, target_team: str | None = None) -> None:
        """Promote a memory to the shared team space."""
        try:
            if not self._coordinator:
                logger.warning("share_memory called but multi-agent mode not enabled")
                return
            self._coordinator.share_memory(memory, target_team)
        except Exception:
            logger.exception("share_memory() failed")

    # --- Observability / Admin ---

    def get_stats(
        self,
        *,
        scan_limit: int = 1000,
    ) -> dict[str, int]:
        """Return memory counts per tier.

        Uses ``MemoryStore.count()`` (O(1) round-trip on SQLite / PG /
        Neo4j, ``collection.count()`` on ChromaDB, ``len()`` on
        InMemory, ``SCAN MATCH`` on Redis) instead of the previous
        ``len(list_all(limit=999999))`` pattern that was O(N) on the
        wire and silently returned ``{}`` on SQLite once the row
        count exceeded ``SQLITE_MAX_VARIABLE_NUMBER``.

        The returned count is capped at ``scan_limit`` so a
        misconfigured backend cannot return an unbounded number; this
        matches the original intent of the limit-based list_all() call
        but does so without paying the O(N) cost.
        """
        try:
            result: dict[str, int] = {}
            for tier, store in self._stores.items():
                try:
                    n = store.count()
                except Exception:
                    logger.exception("get_stats: store.count() failed for tier %s", tier.name)
                    n = 0
                result[tier.name] = min(n, scan_limit) if scan_limit > 0 else n
            return result
        except Exception:
            logger.exception("get_stats() failed")
            return {}

    def get_event_history(self, n: int = 50) -> list[AgentEvent]:
        """Return recent events from the event bus."""
        try:
            return self._bus.get_recent(n)
        except Exception:
            logger.exception("get_event_history() failed")
            return []

    def clear(self) -> None:
        """Clear all memories and event history. Use with caution."""
        try:
            for store in self._stores.values():
                for mem in store.list_all(limit=999999):
                    store.delete(str(mem.id))
            self._bus.clear()
            with self._session_lock:
                self._session_events.clear()
            self._dedup.clear()
            logger.info("UAMS cleared all memories and events")
        except Exception:
            logger.exception("clear() failed")

    # --- Internal ---

    def _consolidate_session(self, session_id: str) -> ConsolidateResult:
        """
        4-tier consolidation: Working -> Episodic -> Semantic -> Procedural.
        Each step is wrapped in try/except to prevent one failure from blocking others.

        Returns a ConsolidateResult; never raises. On a fatal early failure
        (no events to consolidate) the result has source_event_count=0 and
        error=None (a no-op is success).
        """
        start = time.monotonic()
        result = ConsolidateResult(session_id=session_id)
        with self._session_lock:
            events = self._session_events.get(session_id, [])
            if not events:
                result.duration_ms = (time.monotonic() - start) * 1000.0
                return result
            # Snapshot events and clear the buffer to prevent double-consolidation
            events_snapshot = list(events)
            del self._session_events[session_id]
        result.source_event_count = len(events_snapshot)

        logger.info(
            "Consolidating session %s with %d events (agent=%s)",
            session_id, len(events_snapshot), events_snapshot[0].agent_context.agent_id
        )

        # 1. Working -> Episodic
        episodic = None
        try:
            episodic = self._compression.compress_working_to_episodic(events_snapshot)
            self._stores[MemoryType.EPISODIC].store(episodic)
            result.episodic_memory_id = str(episodic.id)
            logger.debug("Episodic memory stored: %s", episodic.id)
        except Exception as exc:
            result.error = f"working_to_episodic: {exc}"
            logger.exception("Working->Episodic consolidation failed for session %s", session_id)

        # 2. Episodic -> Semantic
        if episodic:
            try:
                facts = self._compression.extract_semantic(episodic)
                for fact in facts:
                    self._stores[MemoryType.SEMANTIC].store(fact)
                result.semantic_facts = len(facts)
                logger.debug("Extracted %d semantic facts from session %s", len(facts), session_id)
            except Exception as exc:
                msg = f"episodic_to_semantic: {exc}"
                result.error = f"{result.error}\n{msg}" if result.error else msg
                logger.exception("Episodic->Semantic extraction failed for session %s", session_id)

        # 3. Working -> Semantic (direct explicit facts)
        try:
            for event in events_snapshot:
                if event.structured_data and "fact" in event.structured_data:
                    self.remember(
                        event.structured_data["fact"],
                        event.agent_context,
                        importance=event.structured_data.get("importance", 5.0),
                        category=event.structured_data.get("category", "general"),
                        privacy=event.privacy,
                    )
        except Exception as exc:
            msg = f"direct_fact_extraction: {exc}"
            result.error = f"{result.error}\n{msg}" if result.error else msg
            logger.exception("Direct fact extraction failed for session %s", session_id)

        # 4. Episodic accumulation -> Procedural
        try:
            all_episodes = self._stores[MemoryType.EPISODIC].search_keywords(
                events_snapshot[0].agent_context.agent_id, k=100
            )
            if len(all_episodes) >= 2:
                procedures = self._compression.extract_procedural(all_episodes)
                for proc in procedures:
                    self._stores[MemoryType.PROCEDURAL].store(proc)
                result.procedural_patterns = len(procedures)
                logger.debug(
                    "Extracted %d procedural patterns for agent %s",
                    len(procedures), events_snapshot[0].agent_context.agent_id
                )
        except Exception as exc:
            msg = f"procedural_extraction: {exc}"
            result.error = f"{result.error}\n{msg}" if result.error else msg
            logger.exception("Procedural extraction failed for session %s", session_id)

        result.duration_ms = (time.monotonic() - start) * 1000.0
        return result

    def _truncate_raw(self, text: str) -> str:
        """Truncate raw text to max_raw_length to prevent OOM and API cost explosions."""
        max_len = self._config.max_raw_length
        if len(text) > max_len:
            logger.warning("Truncated raw text from %d to %d chars (max_raw_length)", len(text), max_len)
            return text[:max_len]
        return text

    def shutdown(self) -> None:
        """Graceful shutdown: persist working memories, close resources."""
        logger.info("UAMS shutting down...")
        try:
            # Persist working memories to episodic before shutdown
            working_mems = self._stores[MemoryType.WORKING].list_all(limit=999999)
            logger.info("Persisting %d working memories to episodic before shutdown", len(working_mems))
            for mem in working_mems:
                try:
                    self._stores[MemoryType.EPISODIC].store(mem)
                except Exception:
                    logger.exception("Failed to persist working memory %s during shutdown", mem.id)
        except Exception:
            logger.exception("Working memory persistence during shutdown failed")

        # Close SQLite connections if applicable
        for tier, store in self._stores.items():
            if hasattr(store, 'close'):
                try:
                    store.close()
                    logger.info("Closed store for tier %s", tier.name)
                except Exception:
                    logger.exception("Failed to close store for tier %s", tier.name)

        logger.info("UAMS shutdown complete")

    def register_signal_handlers(self) -> None:
        """Register SIGTERM and SIGINT handlers for graceful shutdown."""
        import signal
        def _signal_handler(signum, frame):
            logger.info("Received signal %d, initiating graceful shutdown...", signum)
            self.shutdown()
            import sys
            sys.exit(0)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        logger.info("Signal handlers registered for graceful shutdown")
