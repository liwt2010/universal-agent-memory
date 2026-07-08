"""Configuration system for UAMS.

Supports environment variables and sensible defaults.
All hardcoded values from the original codebase are now configurable.

Production-Safety:
- `environment` controls validation strictness. In ``production`` mode the
  validator refuses to start with default credentials on credentialed backends
  (Neo4j / PostgreSQL / Redis) and requires TLS. ``staging`` mode logs the same
  issues as warnings but does not refuse.
- ``default_privacy_level`` controls the default PrivacyLevel applied when an
  agent context omits one (PUBLIC / INTERNAL / PRIVATE / SECRET).
- Privacy patterns ship a safe-by-default set inside ``PrivacyFilter`` itself;
  ``privacy_patterns`` here is an *override* channel, not the default.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
import os


# Backends that require credentials and TLS in production.
CREDENTIALED_BACKENDS = frozenset({"neo4j", "postgresql", "redis"})


# Insecure default credential values that must NOT appear in production.
# Keys are config attribute names, values are the unsafe defaults to reject.
INSECURE_DEFAULT_CREDENTIALS = {
    "neo4j_password": frozenset({"", "password", "neo4j", "neo4j_password"}),
    "postgresql_user": frozenset({"uams", "postgres", "admin"}),
    "postgresql_password": frozenset({"", "uams", "postgres", "password", "admin"}),
    "redis_password": frozenset({""}),
}


@dataclass(frozen=True)
class UAMSConfig:
    """Production-grade configuration for Universal Agent Memory System.

    Defaults are safe for local development. Switch ``environment`` to
    ``production`` (via ``UAMS_ENVIRONMENT``) to enable strict checks.
    """

    # --- Environment ---
    environment: str = "development"  # "development" | "staging" | "production"

    # --- Event Bus ---
    event_bus_max_buffer: int = 1000
    max_session_events: int = 10000  # Safety cap: max events tracked per session

    # --- Working Tier TTL ---
    working_ttl_seconds: float = 1800.0  # 30 minutes

    # --- Tier Half-Lives (Ebbinghaus decay) ---
    episodic_half_life_seconds: float = 7 * 24 * 3600  # 7 days
    semantic_half_life_seconds: float = 90 * 24 * 3600  # 90 days
    procedural_half_life_seconds: float = 365 * 24 * 3600  # 1 year

    # --- Deduplication ---
    dedup_window_seconds: float = 300.0

    # --- Retrieval ---
    rrf_k: int = 60
    max_results_per_session: int = 3
    default_token_budget: int = 2000

    # --- Privacy ---
    privacy_patterns: Optional[List[Tuple[str, str]]] = None  # Override PrivacyFilter.DEFAULT_PATTERNS if set
    default_privacy_level: str = "internal"  # PUBLIC | INTERNAL | PRIVATE | SECRET
    privacy_redaction_enabled: bool = True

    # --- Storage Limits ---
    max_raw_length: int = 10000  # Max characters per raw payload
    memory_capacity: int = 10000  # Max in-memory entries (LRU eviction)
    histogram_max_entries: int = 10000  # Max metrics histogram entries before aggregation

    # --- Storage ---
    storage_backend: str = "memory"  # "memory" | "sqlite" | "chromadb" | "redis" | "neo4j" | "postgresql"
    sqlite_path: str = "uams.db"

    # --- SQLite Connection Pool ---
    sqlite_pool_size: int = 5

    # --- Connection / Timeouts ---
    connection_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0

    # --- TLS ---
    redis_use_tls: bool = False
    neo4j_use_tls: bool = False
    postgresql_use_tls: bool = False

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    redis_key_prefix: str = "uams:memory:"
    redis_ttl_seconds: Optional[float] = None
    redis_enable_pubsub: bool = False
    redis_pool_max_connections: int = 50

    # --- Neo4j ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    neo4j_database: str = "neo4j"
    neo4j_ttl_seconds: Optional[float] = None

    # --- PostgreSQL ---
    postgresql_host: str = "localhost"
    postgresql_port: int = 5432
    postgresql_database: str = "uams"
    postgresql_user: str = "uams"
    postgresql_password: str = "uams"
    postgresql_table: str = "uams_memories"
    postgresql_pool_min: int = 1
    postgresql_pool_max: int = 10

    # --- Embedding Provider (optional) ---
    embedding_enabled: bool = False
    embedding_provider: str = "noop"  # "noop" | "sentence_transformers" | "openai_compatible"
    embedding_model: str = "all-MiniLM-L6-v2"  # local default
    embedding_remote_model: str = "text-embedding-3-small"
    embedding_api_key: Optional[str] = None
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_dimension: int = 384  # all-MiniLM-L6-v2 default; set explicitly when changing provider
    embedding_timeout_seconds: float = 10.0
    embedding_max_retries: int = 2
    embedding_batch_size: int = 32
    embedding_cache_enabled: bool = True
    embedding_cache_max_entries: int = 5000
    embedding_device: Optional[str] = None  # "cuda" / "cpu" / None for auto

    # --- LLM Compression (optional) ---
    llm_enabled: bool = False
    llm_provider: str = "openai_compatible"  # "openai_compatible" | "null"
    llm_api_key: Optional[str] = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.0
    llm_cache_enabled: bool = True
    llm_cache_max_entries: int = 1000
    llm_compression_max_events: int = 20
    llm_compression_target_ratio: float = 0.3

    # --- Audit / Observability ---
    enable_audit_log: bool = False
    audit_log_path: Optional[str] = None
    enable_metrics: bool = True

    # --- Identity Field Limits ---
    max_agent_id_length: int = 256
    max_user_id_length: int = 256

    # --- Logging ---
    log_level: str = "INFO"
    structured_logging: bool = True

    # --- Health Check ---
    health_check_port: int = 3111

    # ------------------------------------------------------------------
    # Environment variable parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _env_str(key: str, default: str) -> str:
        raw = os.getenv(key)
        return default if raw is None else raw

    @staticmethod
    def _env_int(key: str, default: int) -> int:
        raw = os.getenv(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be an int, got {raw!r}") from exc

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        raw = os.getenv(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be a float, got {raw!r}") from exc

    @staticmethod
    def _env_optional_float(key: str) -> Optional[float]:
        raw = os.getenv(key)
        if raw is None or raw == "" or raw == "0":
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be a float, got {raw!r}") from exc

    @staticmethod
    def _env_bool(key: str, default: bool) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    # ------------------------------------------------------------------
    # from_env / validate
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "UAMSConfig":
        """Build configuration from environment variables."""
        return cls(
            environment=cls._env_str("UAMS_ENVIRONMENT", "development"),
            event_bus_max_buffer=cls._env_int("UAMS_EVENT_BUS_MAX_BUFFER", 1000),
            max_session_events=cls._env_int("UAMS_MAX_SESSION_EVENTS", 10000),
            working_ttl_seconds=cls._env_float("UAMS_WORKING_TTL", 1800.0),
            episodic_half_life_seconds=cls._env_float("UAMS_EPISODIC_HALFLIFE", 7 * 24 * 3600),
            semantic_half_life_seconds=cls._env_float("UAMS_SEMANTIC_HALFLIFE", 90 * 24 * 3600),
            procedural_half_life_seconds=cls._env_float("UAMS_PROCEDURAL_HALFLIFE", 365 * 24 * 3600),
            dedup_window_seconds=cls._env_float("UAMS_DEDUP_WINDOW", 300.0),
            rrf_k=cls._env_int("UAMS_RRF_K", 60),
            max_results_per_session=cls._env_int("UAMS_MAX_PER_SESSION", 3),
            default_token_budget=cls._env_int("UAMS_DEFAULT_BUDGET", 2000),
            max_raw_length=cls._env_int("UAMS_MAX_RAW_LENGTH", 10000),
            memory_capacity=cls._env_int("UAMS_MEMORY_CAPACITY", 10000),
            histogram_max_entries=cls._env_int("UAMS_HISTOGRAM_MAX_ENTRIES", 10000),
            storage_backend=cls._env_str("UAMS_STORAGE_BACKEND", "memory"),
            sqlite_path=cls._env_str("UAMS_SQLITE_PATH", "uams.db"),
            connection_timeout_seconds=cls._env_float("UAMS_CONNECT_TIMEOUT", 5.0),
            read_timeout_seconds=cls._env_float("UAMS_READ_TIMEOUT", 30.0),
            redis_use_tls=cls._env_bool("UAMS_REDIS_TLS", False),
            neo4j_use_tls=cls._env_bool("UAMS_NEO4J_TLS", False),
            postgresql_use_tls=cls._env_bool("UAMS_PG_TLS", False),
            redis_host=cls._env_str("UAMS_REDIS_HOST", "localhost"),
            redis_port=cls._env_int("UAMS_REDIS_PORT", 6379),
            redis_db=cls._env_int("UAMS_REDIS_DB", 0),
            redis_password=os.getenv("UAMS_REDIS_PASSWORD", None),
            redis_key_prefix=cls._env_str("UAMS_REDIS_PREFIX", "uams:memory:"),
            redis_ttl_seconds=cls._env_optional_float("UAMS_REDIS_TTL"),
            redis_enable_pubsub=cls._env_bool("UAMS_REDIS_PUBSUB", False),
            redis_pool_max_connections=cls._env_int("UAMS_REDIS_POOL_MAX", 50),
            neo4j_uri=cls._env_str("UAMS_NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=cls._env_str("UAMS_NEO4J_USER", "neo4j"),
            neo4j_password=os.getenv("UAMS_NEO4J_PASSWORD", "password"),
            neo4j_database=cls._env_str("UAMS_NEO4J_DATABASE", "neo4j"),
            neo4j_ttl_seconds=cls._env_optional_float("UAMS_NEO4J_TTL"),
            postgresql_host=cls._env_str("UAMS_POSTGRESQL_HOST", "localhost"),
            postgresql_port=cls._env_int("UAMS_POSTGRESQL_PORT", 5432),
            postgresql_database=cls._env_str("UAMS_POSTGRESQL_DATABASE", "uams"),
            postgresql_user=cls._env_str("UAMS_POSTGRESQL_USER", "uams"),
            postgresql_password=os.getenv("UAMS_POSTGRESQL_PASSWORD", "uams"),
            postgresql_table=cls._env_str("UAMS_POSTGRESQL_TABLE", "uams_memories"),
            postgresql_pool_min=cls._env_int("UAMS_POSTGRESQL_POOL_MIN", 1),
            postgresql_pool_max=cls._env_int("UAMS_POSTGRESQL_POOL_MAX", 10),
            embedding_enabled=cls._env_bool("UAMS_EMBEDDING_ENABLED", False),
            embedding_provider=cls._env_str("UAMS_EMBEDDING_PROVIDER", "noop"),
            embedding_model=cls._env_str("UAMS_EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
            embedding_remote_model=cls._env_str("UAMS_EMBEDDING_REMOTE_MODEL", "text-embedding-3-small"),
            embedding_api_key=os.getenv("UAMS_EMBEDDING_API_KEY", None),
            embedding_base_url=cls._env_str("UAMS_EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
            embedding_dimension=cls._env_int("UAMS_EMBEDDING_DIMENSION", 384),
            embedding_timeout_seconds=cls._env_float("UAMS_EMBEDDING_TIMEOUT", 10.0),
            embedding_max_retries=cls._env_int("UAMS_EMBEDDING_MAX_RETRIES", 2),
            embedding_batch_size=cls._env_int("UAMS_EMBEDDING_BATCH_SIZE", 32),
            embedding_cache_enabled=cls._env_bool("UAMS_EMBEDDING_CACHE", True),
            embedding_cache_max_entries=cls._env_int("UAMS_EMBEDDING_CACHE_MAX", 5000),
            embedding_device=os.getenv("UAMS_EMBEDDING_DEVICE", None),
            llm_enabled=cls._env_bool("UAMS_LLM_ENABLED", False),
            llm_provider=cls._env_str("UAMS_LLM_PROVIDER", "openai_compatible"),
            llm_api_key=os.getenv("UAMS_LLM_API_KEY", None),
            llm_base_url=cls._env_str("UAMS_LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_model=cls._env_str("UAMS_LLM_MODEL", "gpt-4o-mini"),
            llm_timeout_seconds=cls._env_float("UAMS_LLM_TIMEOUT", 30.0),
            llm_max_retries=cls._env_int("UAMS_LLM_MAX_RETRIES", 2),
            llm_max_tokens=cls._env_int("UAMS_LLM_MAX_TOKENS", 1024),
            llm_temperature=cls._env_float("UAMS_LLM_TEMPERATURE", 0.0),
            llm_cache_enabled=cls._env_bool("UAMS_LLM_CACHE", True),
            llm_cache_max_entries=cls._env_int("UAMS_LLM_CACHE_MAX", 1000),
            llm_compression_max_events=cls._env_int("UAMS_LLM_COMPRESS_MAX_EVENTS", 20),
            llm_compression_target_ratio=cls._env_float("UAMS_LLM_TARGET_RATIO", 0.3),
            enable_audit_log=cls._env_bool("UAMS_AUDIT_LOG", False),
            audit_log_path=os.getenv("UAMS_AUDIT_LOG_PATH", None),
            enable_metrics=cls._env_bool("UAMS_METRICS", True),
            max_agent_id_length=cls._env_int("UAMS_MAX_AGENT_ID_LENGTH", 256),
            max_user_id_length=cls._env_int("UAMS_MAX_USER_ID_LENGTH", 256),
            log_level=cls._env_str("UAMS_LOG_LEVEL", "INFO"),
            structured_logging=cls._env_bool("UAMS_STRUCTURED_LOG", True),
            health_check_port=cls._env_int("UAMS_HEALTH_PORT", 3111),
        )

    def validate(self) -> None:
        """Validate configuration constraints. Raises ValueError if invalid.

        Strictness by ``environment``:
        - ``development``: structural checks only (no warnings, no production
          checks).
        - ``staging``: structural checks + warn on insecure defaults (logs).
        - ``production``: structural checks + ERROR on insecure defaults
          (raises ValueError, refusing to start).
        """
        errors: List[str] = []

        # --- Environment ---
        if self.environment not in ("development", "staging", "production"):
            errors.append(
                f"environment must be one of development|staging|production, "
                f"got {self.environment!r}"
            )

        # --- Basic bounds ---
        if self.event_bus_max_buffer < 1:
            errors.append("event_bus_max_buffer must be >= 1")
        if self.max_session_events < 1:
            errors.append("max_session_events must be >= 1")
        if self.working_ttl_seconds < 0:
            errors.append("working_ttl_seconds must be >= 0")
        if self.dedup_window_seconds < 0:
            errors.append("dedup_window_seconds must be >= 0")
        if self.rrf_k < 1:
            errors.append("rrf_k must be >= 1")
        if self.max_results_per_session < 1:
            errors.append("max_results_per_session must be >= 1")
        if self.default_token_budget < 100:
            errors.append("default_token_budget must be >= 100")
        if self.max_raw_length < 1:
            errors.append("max_raw_length must be >= 1")
        if self.memory_capacity < 1:
            errors.append("memory_capacity must be >= 1")
        if self.histogram_max_entries < 1:
            errors.append("histogram_max_entries must be >= 1")

        # --- Half-life bounds (60s min, 10y max) ---
        min_hl, max_hl = 60.0, 10 * 365 * 24 * 3600.0
        for field_name in (
            "working_ttl_seconds",
            "episodic_half_life_seconds",
            "semantic_half_life_seconds",
            "procedural_half_life_seconds",
        ):
            value = getattr(self, field_name)
            if value < min_hl or value > max_hl:
                errors.append(
                    f"{field_name} must be between {min_hl:.0f}s and {max_hl:.0f}s (got {value})"
                )

        # --- Privacy level ---
        valid_levels = ("public", "internal", "private", "secret")
        if self.default_privacy_level.lower() not in valid_levels:
            errors.append(
                f"default_privacy_level must be one of {valid_levels} (case-insensitive), "
                f"got {self.default_privacy_level!r}"
            )

        # --- Timeouts ---
        if self.connection_timeout_seconds < 0.1 or self.connection_timeout_seconds > 60:
            errors.append("connection_timeout_seconds must be between 0.1 and 60")
        if self.read_timeout_seconds < 1 or self.read_timeout_seconds > 600:
            errors.append("read_timeout_seconds must be between 1 and 600")

        # --- Identity limits ---
        if self.max_agent_id_length < 1 or self.max_agent_id_length > 4096:
            errors.append("max_agent_id_length must be between 1 and 4096")
        if self.max_user_id_length < 1 or self.max_user_id_length > 4096:
            errors.append("max_user_id_length must be between 1 and 4096")

        # --- Storage backend ---
        valid_backends = ("memory", "sqlite", "chromadb", "redis", "neo4j", "postgresql")
        if self.storage_backend not in valid_backends:
            errors.append(f"invalid storage_backend: {self.storage_backend}")

        # --- Storage backend coupling ---
        if self.storage_backend == "sqlite" and not self.sqlite_path:
            errors.append("sqlite_path must be set when storage_backend=sqlite")

        # --- Log level ---
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            errors.append(f"invalid log_level: {self.log_level}")

        # --- Health check port ---
        if self.health_check_port < 1 or self.health_check_port > 65535:
            errors.append("health_check_port must be between 1 and 65535")

        # --- PG pool ---
        if self.postgresql_pool_min < 1:
            errors.append("postgresql_pool_min must be >= 1")
        if self.postgresql_pool_max < self.postgresql_pool_min:
            errors.append("postgresql_pool_max must be >= postgresql_pool_min")

        # --- LLM Compression ---
        if self.llm_provider not in ("openai_compatible", "null"):
            errors.append(
                f"llm_provider must be openai_compatible|null, got {self.llm_provider!r}"
            )
        if self.llm_enabled and not self.llm_api_key:
            errors.append("llm_api_key is required when llm_enabled=True")
        if self.llm_timeout_seconds < 1 or self.llm_timeout_seconds > 300:
            errors.append("llm_timeout_seconds must be between 1 and 300")
        if self.llm_max_tokens < 64 or self.llm_max_tokens > 8192:
            errors.append("llm_max_tokens must be between 64 and 8192")
        if self.llm_temperature < 0.0 or self.llm_temperature > 2.0:
            errors.append("llm_temperature must be between 0.0 and 2.0")
        if self.llm_cache_max_entries < 1:
            errors.append("llm_cache_max_entries must be >= 1")
        if self.llm_compression_max_events < 1 or self.llm_compression_max_events > 200:
            errors.append("llm_compression_max_events must be between 1 and 200")
        if self.llm_compression_target_ratio <= 0 or self.llm_compression_target_ratio > 1.0:
            errors.append("llm_compression_target_ratio must be in (0, 1.0]")

        # --- Embedding Provider ---
        valid_embedding_providers = ("noop", "sentence_transformers", "openai_compatible")
        if self.embedding_provider not in valid_embedding_providers:
            errors.append(
                f"embedding_provider must be one of {valid_embedding_providers}, "
                f"got {self.embedding_provider!r}"
            )
        if self.embedding_enabled and self.embedding_provider == "noop":
            errors.append(
                "embedding_enabled=True requires embedding_provider != noop "
                "(use sentence_transformers or openai_compatible)"
            )
        if self.embedding_enabled and self.embedding_provider == "openai_compatible":
            if not self.embedding_api_key:
                errors.append("embedding_api_key is required when embedding_provider=openai_compatible")
        if self.embedding_dimension < 1 or self.embedding_dimension > 8192:
            errors.append("embedding_dimension must be between 1 and 8192")
        if self.embedding_timeout_seconds < 1 or self.embedding_timeout_seconds > 120:
            errors.append("embedding_timeout_seconds must be between 1 and 120")
        if self.embedding_batch_size < 1 or self.embedding_batch_size > 2048:
            errors.append("embedding_batch_size must be between 1 and 2048")
        if self.embedding_cache_max_entries < 1:
            errors.append("embedding_cache_max_entries must be >= 1")

        # --- Audit log coupling ---
        if self.enable_audit_log and not self.audit_log_path:
            errors.append("audit_log_path must be set when enable_audit_log=True")

        # --- Production / staging safety ---
        if self.environment in ("staging", "production"):
            production_errors = self._check_insecure_defaults()
            if self.environment == "production":
                errors.extend(production_errors)
            else:
                # staging: warn only (do not raise)
                if production_errors:
                    import logging
                    logging.getLogger(__name__).warning(
                        "UAMSConfig staging-mode warnings: %s",
                        "; ".join(production_errors),
                    )

        if errors:
            raise ValueError(f"Configuration validation failed: {'; '.join(errors)}")

    # ------------------------------------------------------------------
    # Production-safety helpers
    # ------------------------------------------------------------------

    def _check_insecure_defaults(self) -> List[str]:
        """Return list of error strings for insecure defaults.

        Called only when environment is staging or production.
        """
        errors: List[str] = []

        if self.storage_backend == "neo4j":
            if self.neo4j_password in INSECURE_DEFAULT_CREDENTIALS["neo4j_password"]:
                errors.append(
                    "neo4j_password is set to an insecure default; "
                    "set UAMS_NEO4J_PASSWORD to a real secret"
                )
            if self.neo4j_uri.startswith("bolt://") and not self.neo4j_use_tls:
                errors.append(
                    "neo4j uses plain bolt://; enable UAMS_NEO4J_TLS=true "
                    "or switch to bolt+s://"
                )

        if self.storage_backend == "postgresql":
            if self.postgresql_password in INSECURE_DEFAULT_CREDENTIALS["postgresql_password"]:
                errors.append(
                    "postgresql_password is set to an insecure default; "
                    "set UAMS_POSTGRESQL_PASSWORD to a real secret"
                )
            if self.postgresql_user in INSECURE_DEFAULT_CREDENTIALS["postgresql_user"]:
                errors.append(
                    "postgresql_user is set to an insecure default; "
                    "set UAMS_POSTGRESQL_USER to a real service account"
                )
            if not self.postgresql_use_tls:
                errors.append(
                    "postgresql connection is plain; enable UAMS_PG_TLS=true"
                )

        if self.storage_backend == "redis":
            if not self.redis_password:
                errors.append(
                    "redis_password is empty; set UAMS_REDIS_PASSWORD to a real secret"
                )
            if not self.redis_use_tls:
                errors.append(
                    "redis connection is plain; enable UAMS_REDIS_TLS=true"
                )

        return errors


# Backward-compatible alias
Config = UAMSConfig