"""Configuration system for UAMS.

Supports environment variables and sensible defaults.
All hardcoded values from the original codebase are now configurable.
"""

from dataclasses import dataclass, field
from typing import Optional, Set
import os


@dataclass(frozen=True)
class UAMSConfig:
    """Production-grade configuration for Universal Agent Memory System."""

    # --- Event Bus ---
    event_bus_max_buffer: int = 1000

    # --- Working Tier TTL ---
    working_ttl_seconds: float = 1800.0  # 30 minutes

    # --- Deduplication ---
    dedup_window_seconds: float = 300.0

    # --- Retrieval ---
    rrf_k: int = 60
    max_results_per_session: int = 3
    default_token_budget: int = 2000

    # --- Privacy ---
    privacy_patterns: Optional[list] = None  # List of (regex, replacement) tuples

    # --- Storage Limits ---
    max_raw_length: int = 10000  # Max characters per raw payload
    memory_capacity: int = 10000  # Max in-memory entries (LRU eviction)
    histogram_max_entries: int = 10000  # Max metrics histogram entries before aggregation

    # --- Storage ---
    storage_backend: str = "memory"  # "memory" | "sqlite" | "chromadb" | "redis" | "neo4j"
    sqlite_path: str = "uams.db"
    
    # --- SQLite Connection Pool ---
    sqlite_pool_size: int = 5  # Connection pool size (not strictly needed for SQLite, but for future)
    
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

    # --- Logging ---
    log_level: str = "INFO"
    structured_logging: bool = True

    # --- Health Check ---
    health_check_port: int = 3111

    @classmethod
    def from_env(cls) -> "UAMSConfig":
        """Build configuration from environment variables."""
        return cls(
            event_bus_max_buffer=int(os.getenv("UAMS_EVENT_BUS_MAX_BUFFER", "1000")),
            working_ttl_seconds=float(os.getenv("UAMS_WORKING_TTL", "1800")),
            dedup_window_seconds=float(os.getenv("UAMS_DEDUP_WINDOW", "300")),
            rrf_k=int(os.getenv("UAMS_RRF_K", "60")),
            max_results_per_session=int(os.getenv("UAMS_MAX_PER_SESSION", "3")),
            default_token_budget=int(os.getenv("UAMS_DEFAULT_BUDGET", "2000")),
            max_raw_length=int(os.getenv("UAMS_MAX_RAW_LENGTH", "10000")),
            memory_capacity=int(os.getenv("UAMS_MEMORY_CAPACITY", "10000")),
            histogram_max_entries=int(os.getenv("UAMS_HISTOGRAM_MAX_ENTRIES", "10000")),
            storage_backend=os.getenv("UAMS_STORAGE_BACKEND", "memory"),
            sqlite_path=os.getenv("UAMS_SQLITE_PATH", "uams.db"),
            redis_host=os.getenv("UAMS_REDIS_HOST", "localhost"),
            redis_port=int(os.getenv("UAMS_REDIS_PORT", "6379")),
            redis_db=int(os.getenv("UAMS_REDIS_DB", "0")),
            redis_password=os.getenv("UAMS_REDIS_PASSWORD", None),
            redis_key_prefix=os.getenv("UAMS_REDIS_PREFIX", "uams:memory:"),
            redis_ttl_seconds=float(os.getenv("UAMS_REDIS_TTL", "0")) or None if os.getenv("UAMS_REDIS_TTL", "0") == "0" else float(os.getenv("UAMS_REDIS_TTL", "0")),
            redis_enable_pubsub=os.getenv("UAMS_REDIS_PUBSUB", "false").lower() == "true",
            redis_pool_max_connections=int(os.getenv("UAMS_REDIS_POOL_MAX", "50")),
            neo4j_uri=os.getenv("UAMS_NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=os.getenv("UAMS_NEO4J_USER", "neo4j"),
            neo4j_password=os.getenv("UAMS_NEO4J_PASSWORD", "password"),
            neo4j_database=os.getenv("UAMS_NEO4J_DATABASE", "neo4j"),
            neo4j_ttl_seconds=float(os.getenv("UAMS_NEO4J_TTL", "0")) or None if os.getenv("UAMS_NEO4J_TTL", "0") == "0" else float(os.getenv("UAMS_NEO4J_TTL", "0")),
            postgresql_host=os.getenv("UAMS_POSTGRESQL_HOST", "localhost"),
            postgresql_port=int(os.getenv("UAMS_POSTGRESQL_PORT", "5432")),
            postgresql_database=os.getenv("UAMS_POSTGRESQL_DATABASE", "uams"),
            postgresql_user=os.getenv("UAMS_POSTGRESQL_USER", "uams"),
            postgresql_password=os.getenv("UAMS_POSTGRESQL_PASSWORD", "uams"),
            postgresql_table=os.getenv("UAMS_POSTGRESQL_TABLE", "uams_memories"),
            postgresql_pool_min=int(os.getenv("UAMS_POSTGRESQL_POOL_MIN", "1")),
            postgresql_pool_max=int(os.getenv("UAMS_POSTGRESQL_POOL_MAX", "10")),
            log_level=os.getenv("UAMS_LOG_LEVEL", "INFO"),
            health_check_port=int(os.getenv("UAMS_HEALTH_PORT", "3111")),
        )

    def validate(self) -> None:
        """Validate configuration constraints. Raises ValueError if invalid."""
        errors = []
        if self.event_bus_max_buffer < 1:
            errors.append("event_bus_max_buffer must be >= 1")
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
        if self.storage_backend not in ("memory", "sqlite", "chromadb", "redis", "neo4j", "postgresql"):
            errors.append(f"invalid storage_backend: {self.storage_backend}")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            errors.append(f"invalid log_level: {self.log_level}")
        if self.health_check_port < 1 or self.health_check_port > 65535:
            errors.append("health_check_port must be between 1 and 65535")
        if self.postgresql_pool_min < 1:
            errors.append("postgresql_pool_min must be >= 1")
        if self.postgresql_pool_max < self.postgresql_pool_min:
            errors.append("postgresql_pool_max must be >= postgresql_pool_min")
        if errors:
            raise ValueError(f"Configuration validation failed: {'; '.join(errors)}")
