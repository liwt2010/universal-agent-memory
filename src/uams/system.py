"""The Universal Memory System (UAMS) - production-ready main facade.

Integrates event bus, tiered storage, compression, retrieval, and multi-agent coordination.
All operations are thread-safe and include error handling with graceful degradation.
"""

import threading
from typing import Callable, Dict, List, Optional

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
from uams.utils.tokens import TokenEstimator, get_default_estimator

logger = get_logger(__name__)


class UniversalMemorySystem(EventHandler):
    """
    The main facade. Any agent framework integrates via this class.

    Thread-safe. Error handling with graceful degradation.
    Configurable via UAMSConfig.
    """

    def __init__(
        self,
        stores: Optional[Dict[MemoryType, MemoryStore]] = None,
        compression: Optional[CompressionEngine] = None,
        embedding_fn: EmbeddingFn = None,
        privacy_filter: Optional[PrivacyFilter] = None,
        dedup_window: Optional[DeduplicationWindow] = None,
        config: Optional[UAMSConfig] = None,
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
        )
        self._forgetting = ForgettingEngine(self._stores)
        self._coordinator: Optional[MultiAgentCoordinator] = None

        # Embedding callable: explicit kwarg wins; otherwise build from config.
        if embedding_fn is not None:
            self._embedding_fn = embedding_fn
        else:
            self._embedding_fn = self._build_embedding_fn()

        # Register ourselves on the event bus for consolidation triggers
        self._bus.subscribe(self, [EventType.SESSION_END, EventType.SUBSESSION_END])

        # Session tracking: session_id -> list of events
        self._session_events: Dict[str, List[AgentEvent]] = {}
        self._session_lock = threading.RLock()

    def _build_compression_engine(self):
        """Construct the compression engine.

        Uses ``LLMCompressionEngine`` when ``llm_enabled=True`` and an API key
        is configured. Falls back to ``HeuristicCompressionEngine`` on any
        initialization failure so the agent loop never stalls.
        """
        if not (self._config.llm_enabled and self._config.llm_api_key):
            return HeuristicCompressionEngine()

        try:
            from uams.llm.client import CachedLLMClient, OpenAICompatibleClient
            from uams.pipeline.llm_compression import LLMCompressionEngine

            inner = OpenAICompatibleClient(
                api_key=self._config.llm_api_key,
                base_url=self._config.llm_base_url,
                model=self._config.llm_model,
                timeout=self._config.llm_timeout_seconds,
                max_retries=self._config.llm_max_retries,
            )
            client = (
                CachedLLMClient(inner, max_entries=self._config.llm_cache_max_entries)
                if self._config.llm_cache_enabled
                else inner
            )
            engine = LLMCompressionEngine(
                client,
                max_events_per_call=self._config.llm_compression_max_events,
                target_ratio=self._config.llm_compression_target_ratio,
                timeout=self._config.llm_timeout_seconds,
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
            from uams.llm.client import CachedLLMClient, OpenAICompatibleClient
            from uams.pipeline.query_rewrite import QueryRewriter

            inner = OpenAICompatibleClient(
                api_key=self._config.llm_api_key,
                base_url=self._config.llm_base_url,
                model=self._config.llm_model,
                timeout=self._config.query_rewrite_timeout_seconds,
                max_retries=self._config.llm_max_retries,
            )
            if self._config.query_rewrite_cache_enabled:
                client = CachedLLMClient(
                    inner, max_entries=self._config.query_rewrite_cache_max_entries
                )
            else:
                client = inner
            return QueryRewriter(
                llm_client=client,
                max_variants=self._config.query_rewrite_max_variants,
                timeout=self._config.query_rewrite_timeout_seconds,
                cache_max_entries=self._config.query_rewrite_cache_max_entries,
            )
        except Exception:
            logger.exception(
                "Failed to initialize QueryRewriter; query rewriting disabled for this session"
            )
            return None

    def _init_stores_from_config(self) -> Dict[MemoryType, MemoryStore]:
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
                    MemoryType.EPISODIC: SQLiteStore(self._config.sqlite_path, "episodic"),
                    MemoryType.SEMANTIC: SQLiteStore(self._config.sqlite_path, "semantic"),
                    MemoryType.PROCEDURAL: SQLiteStore(self._config.sqlite_path, "procedural"),
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
        # 1. Publish to event bus (notifies all subscribers)
        try:
            self._bus.publish(event)
        except Exception:
            logger.exception("EventBus publish failed for event %s. Continuing.", event.event_id)

        # 2. Track for session consolidation (with lock)
        try:
            with self._session_lock:
                sid = event.agent_context.session_id
                if sid not in self._session_events:
                    self._session_events[sid] = []
                self._session_events[sid].append(event)
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
        tags: Optional[set] = None,
    ) -> Optional[MemoryId]:
        """
        Explicitly save a fact/preference/pattern to semantic memory.
        Returns MemoryId on success, None on failure (graceful degradation).
        """
        try:
            truncated_fact = self._truncate_raw(fact)
            safe_fact = InputValidator.sanitize_all(truncated_fact, max_length=self._config.max_raw_length)
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(),
                context=context,
                payload=MemoryPayload(
                    raw=safe_fact,
                    structured={"explicit": True, "category": category},
                ),
                metadata=MemoryMetadata(
                    memory_type=MemoryType.SEMANTIC,
                    privacy=privacy,
                    importance=importance,
                    categories={category} | (tags or set()),
                ),
            )

            if self._embedding_fn:
                try:
                    mem.payload.embedding = self._embedding_fn(truncated_fact)
                except Exception:
                    logger.exception("Embedding failed for fact '%s...'. Storing without embedding.", truncated_fact[:50])
                    # Continue without embedding - BM25 fallback still works

            self._stores[MemoryType.SEMANTIC].store(mem)
            logger.info(
                "Remembered fact for agent=%s session=%s category=%s",
                context.agent_id, context.session_id, category
            )
            return mem.id
        except Exception:
            logger.exception("remember() failed. Fact not stored.")
            return None

    def recall(
        self,
        query: str,
        context: AgentContext,
        budget_tokens: int = None,
        include_working: bool = True,
    ) -> List[Memory]:
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

    def forget(self, memory_id: str) -> bool:
        """
        Delete a specific memory by ID.
        Returns True if found and deleted.
        """
        try:
            for tier, store in self._stores.items():
                mem = store.retrieve(memory_id)
                if mem:
                    store.delete(memory_id)
                    logger.info(
                        "Forgot memory %s from tier %s (agent=%s, user=%s)",
                        memory_id, tier.name, mem.context.agent_id, mem.context.user_id
                    )
                    return True
            logger.warning("forget() called for non-existent memory_id=%s", memory_id)
            return False
        except Exception:
            logger.exception("forget() failed for memory_id=%s", memory_id)
            return False

    def consolidate(self, session_id: Optional[str] = None) -> None:
        """
        Manually trigger 4-tier memory consolidation.
        If no session_id provided, consolidates all pending sessions.
        """
        try:
            if session_id:
                self._consolidate_session(session_id)
            else:
                with self._session_lock:
                    sids = list(self._session_events.keys())
                for sid in sids:
                    self._consolidate_session(sid)
        except Exception:
            logger.exception("consolidate() failed")

    def get_session_summary(self, session_id: str) -> Optional[Memory]:
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
        """Run forgetting sweep. Returns count of deleted memories."""
        try:
            count = self._forgetting.sweep()
            logger.info("decay_sweep() evicted %d memories", count)
            return count
        except Exception:
            logger.exception("decay_sweep() failed")
            return 0

    # --- Multi-agent primitives ---

    def enable_multi_agent(self, shared_store: Optional[MemoryStore] = None) -> None:
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

    def read_signals(self, agent_id: str) -> List[Signal]:
        """Read all unread signals for this agent."""
        try:
            if not self._coordinator:
                return []
            return self._coordinator.read_signals(agent_id)
        except Exception:
            logger.exception("read_signals() failed")
            return []

    def share_memory(self, memory: Memory, target_team: Optional[str] = None) -> None:
        """Promote a memory to the shared team space."""
        try:
            if not self._coordinator:
                logger.warning("share_memory called but multi-agent mode not enabled")
                return
            self._coordinator.share_memory(memory, target_team)
        except Exception:
            logger.exception("share_memory() failed")

    # --- Observability / Admin ---

    def get_stats(self) -> Dict[str, int]:
        """Return memory counts per tier."""
        try:
            return {
                tier.name: len(store.list_all(limit=999999))
                for tier, store in self._stores.items()
            }
        except Exception:
            logger.exception("get_stats() failed")
            return {}

    def get_event_history(self, n: int = 50) -> List[AgentEvent]:
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

    def _consolidate_session(self, session_id: str) -> None:
        """
        4-tier consolidation: Working -> Episodic -> Semantic -> Procedural.
        Each step is wrapped in try/except to prevent one failure from blocking others.
        """
        with self._session_lock:
            events = self._session_events.get(session_id, [])
            if not events:
                return
            # Snapshot events and clear the buffer to prevent double-consolidation
            events_snapshot = list(events)
            del self._session_events[session_id]

        logger.info(
            "Consolidating session %s with %d events (agent=%s)",
            session_id, len(events_snapshot), events_snapshot[0].agent_context.agent_id
        )

        # 1. Working -> Episodic
        try:
            episodic = self._compression.compress_working_to_episodic(events_snapshot)
            self._stores[MemoryType.EPISODIC].store(episodic)
            logger.debug("Episodic memory stored: %s", episodic.id)
        except Exception:
            logger.exception("Working->Episodic consolidation failed for session %s", session_id)
            episodic = None

        # 2. Episodic -> Semantic
        if episodic:
            try:
                facts = self._compression.extract_semantic(episodic)
                for fact in facts:
                    self._stores[MemoryType.SEMANTIC].store(fact)
                logger.debug("Extracted %d semantic facts from session %s", len(facts), session_id)
            except Exception:
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
        except Exception:
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
                logger.debug(
                    "Extracted %d procedural patterns for agent %s",
                    len(procedures), events_snapshot[0].agent_context.agent_id
                )
        except Exception:
            logger.exception("Procedural extraction failed for session %s", session_id)

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
