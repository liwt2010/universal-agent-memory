"""Storage layer for UAMS. Provides pluggable backends for each memory tier."""

from uams.storage.base import MemoryStore
from uams.storage.memory import InMemoryStore

# Optional backends - import gracefully to avoid hard dependencies

try:
    from uams.storage.sqlite import SQLiteStore
except ImportError:
    SQLiteStore = None  # type: ignore[misc,assignment]

try:
    from uams.storage.chromadb import ChromaDBStore
except ImportError:
    ChromaDBStore = None  # type: ignore[misc,assignment]

try:
    from uams.storage.redis import RedisStore
except ImportError:
    RedisStore = None  # type: ignore[misc,assignment]

try:
    from uams.storage.neo4j import Neo4jStore
except ImportError:
    Neo4jStore = None  # type: ignore[misc,assignment]

try:
    from uams.storage.postgresql import PostgreSQLStore
except ImportError:
    PostgreSQLStore = None  # type: ignore[misc,assignment]

__all__ = [
    "MemoryStore",
    "InMemoryStore",
    "SQLiteStore",
    "ChromaDBStore",
    "RedisStore",
    "Neo4jStore",
    "PostgreSQLStore",
]
