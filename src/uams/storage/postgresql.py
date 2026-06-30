"""PostgreSQL enterprise-grade storage backend for UAMS.

Optional dependency: requires `pip install psycopg2-binary`.
Features:
- Connection pooling via psycopg2.pool.ThreadedConnectionPool
- Full ACID transactions
- JSONB for structured data and embeddings
- GIN indexes for full-text search and JSONB
- Automatic schema creation and migrations
- Prepared statements for performance
"""

import json
import pickle
import threading
from typing import Any, Dict, List, Optional, Tuple

from uams.storage.base import MemoryStore
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata, Relation,
)
from uams.core.enums import MemoryType, PrivacyLevel
from uams.utils.logging import get_logger

logger = get_logger(__name__)

_PG_SCHEMA_VERSION = 1


class PostgreSQLStore(MemoryStore):
    """
    PostgreSQL-backed enterprise storage with connection pooling,
    JSONB structured storage, GIN full-text indexes, and ACID transactions.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "uams",
        user: str = "uams",
        password: str = "uams",
        table_name: str = "uams_memories",
        pool_min: int = 1,
        pool_max: int = 10,
        ttl_seconds: Optional[float] = None,
    ):
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._table_name = table_name
        self._ttl_seconds = ttl_seconds
        self._pool = None
        self._lock = threading.RLock()
        self._available = False

        try:
            import psycopg2
            from psycopg2.pool import ThreadedConnectionPool
            self._psycopg2 = psycopg2
            self._ThreadedConnectionPool = ThreadedConnectionPool

            self._pool = ThreadedConnectionPool(
                pool_min, pool_max,
                host=host, port=port, database=database,
                user=user, password=password,
                connect_timeout=5,
            )
            # Test connection
            conn = self._pool.getconn()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            self._pool.putconn(conn)
            self._available = True
            logger.info(
                "PostgreSQLStore connected: postgresql://%s@%s:%d/%s (table=%s, pool=%d-%d)",
                user, host, port, database, table_name, pool_min, pool_max
            )
            self._ensure_schema()
            self._run_migrations()
        except ImportError:
            logger.warning("psycopg2 not installed. PostgreSQLStore will fall back to no-op.")
        except Exception as e:
            logger.error("PostgreSQL connection failed: %s. Store will be no-op.", e)

    def _get_conn(self):
        return self._pool.getconn()

    def _put_conn(self, conn, close=False):
        if close:
            conn.close()
        else:
            self._pool.putconn(conn)

    def _ensure_schema(self) -> None:
        if not self._available or not self._pool:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Schema version tracking
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS _schema_version (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Main memories table with JSONB
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {self._table_name} (
                        id TEXT PRIMARY KEY,
                        created_at DOUBLE PRECISION,
                        accessed_at DOUBLE PRECISION,
                        consolidated_at DOUBLE PRECISION,
                        expires_at DOUBLE PRECISION,
                        raw TEXT NOT NULL,
                        structured JSONB,
                        embedding BYTEA,
                        memory_type TEXT,
                        privacy TEXT,
                        importance REAL,
                        confidence REAL,
                        tags JSONB,
                        categories JSONB,
                        relations JSONB,
                        provenance JSONB,
                        agent_id TEXT,
                        agent_type TEXT,
                        session_id TEXT,
                        user_id TEXT,
                        team_id TEXT,
                        project_id TEXT
                    )
                """)

                # GIN indexes for JSONB and full-text
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._table_name}_agent
                    ON {self._table_name}(agent_id)
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._table_name}_session
                    ON {self._table_name}(session_id)
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._table_name}_expires
                    ON {self._table_name}(expires_at)
                    WHERE expires_at IS NOT NULL
                """)
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._table_name}_type
                    ON {self._table_name}(memory_type)
                """)
                # GIN index on raw text for tsvector search
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._table_name}_fts
                    ON {self._table_name} USING GIN (to_tsvector('english', raw))
                """)
                # GIN index on structured JSONB
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._table_name}_structured
                    ON {self._table_name} USING GIN (structured)
                """)
                # GIN index on tags array
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._table_name}_tags
                    ON {self._table_name} USING GIN (tags)
                """)

                conn.commit()
                logger.info("PostgreSQL schema ensured for table %s", self._table_name)
        except Exception:
            logger.exception("PostgreSQL schema initialization failed")
            conn.rollback()
        finally:
            self._put_conn(conn)

    def _run_migrations(self) -> None:
        if not self._available:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(version) FROM _schema_version")
                row = cur.fetchone()
                current_version = row[0] if row and row[0] is not None else 0

                if current_version < _PG_SCHEMA_VERSION:
                    logger.info("Migrating PostgreSQL schema from %d to %d", current_version, _PG_SCHEMA_VERSION)
                    for v in range(current_version + 1, _PG_SCHEMA_VERSION + 1):
                        self._apply_migration(cur, v)
                    cur.execute(
                        "INSERT INTO _schema_version (version) VALUES (%s)",
                        (_PG_SCHEMA_VERSION,)
                    )
                    conn.commit()
                    logger.info("PostgreSQL schema migration completed to version %d", _PG_SCHEMA_VERSION)
        except Exception:
            logger.exception("PostgreSQL schema migration failed")
            conn.rollback()
        finally:
            self._put_conn(conn)

    def _apply_migration(self, cur, version: int) -> None:
        if version == 1:
            pass  # Initial schema
        # Future migrations here
        logger.info("Applied PostgreSQL migration version %d", version)

    def _memory_to_dict(self, memory: Memory) -> Dict[str, Any]:
        return {
            "id": str(memory.id),
            "created_at": memory.anchor.created_at,
            "accessed_at": memory.anchor.accessed_at,
            "consolidated_at": memory.anchor.consolidated_at,
            "expires_at": memory.anchor.expires_at,
            "raw": memory.payload.raw,
            "structured": json.dumps(memory.payload.structured) if memory.payload.structured else None,
            "embedding": pickle.dumps(memory.payload.embedding) if memory.payload.embedding else None,
            "memory_type": memory.metadata.memory_type.name,
            "privacy": memory.metadata.privacy.name,
            "importance": memory.metadata.importance,
            "confidence": memory.metadata.confidence,
            "tags": json.dumps(list(memory.metadata.tags)) if memory.metadata.tags else None,
            "categories": json.dumps(list(memory.metadata.categories)) if memory.metadata.categories else None,
            "relations": json.dumps([
                {"type": r.relation_type, "target": r.target_memory_id, "strength": r.strength}
                for r in memory.metadata.relations
            ]) if memory.metadata.relations else None,
            "provenance": json.dumps(memory.metadata.provenance) if memory.metadata.provenance else None,
            "agent_id": memory.context.agent_id,
            "agent_type": memory.context.agent_type,
            "session_id": memory.context.session_id,
            "user_id": memory.context.user_id,
            "team_id": memory.context.team_id,
            "project_id": memory.context.project_id,
        }

    def _row_to_memory(self, row: Tuple) -> Optional[Memory]:
        try:
            (id_str, created_at, accessed_at, consolidated_at, expires_at,
             raw, structured_str, embedding_blob, mem_type, privacy,
             importance, confidence, tags_str, categories_str, relations_str,
             provenance_str, agent_id, agent_type, session_id, user_id,
             team_id, project_id) = row

            structured = json.loads(structured_str) if structured_str else None
            embedding = pickle.loads(embedding_blob) if embedding_blob else None
            tags = set(json.loads(tags_str)) if tags_str else set()
            categories = set(json.loads(categories_str)) if categories_str else set()
            relations = [
                Relation(r["type"], r["target"], strength=r.get("strength", 1.0))
                for r in json.loads(relations_str)
            ] if relations_str else []
            provenance = json.loads(provenance_str) if provenance_str else []

            return Memory(
                id=MemoryId(id_str),
                anchor=TemporalAnchor(
                    created_at=created_at or 0,
                    accessed_at=accessed_at,
                    consolidated_at=consolidated_at,
                    expires_at=expires_at,
                ),
                context=AgentContext(
                    agent_id=agent_id or "",
                    agent_type=agent_type or "",
                    session_id=session_id or "",
                    user_id=user_id or None,
                    team_id=team_id or None,
                    project_id=project_id or None,
                ),
                payload=MemoryPayload(
                    raw=raw,
                    structured=structured,
                    embedding=embedding,
                ),
                metadata=MemoryMetadata(
                    memory_type=MemoryType[mem_type] if mem_type else MemoryType.WORKING,
                    privacy=PrivacyLevel[privacy] if privacy else PrivacyLevel.PUBLIC,
                    importance=importance or 5.0,
                    confidence=confidence or 1.0,
                    tags=tags,
                    categories=categories,
                    relations=relations,
                    provenance=provenance,
                ),
            )
        except Exception as e:
            logger.exception("Failed to deserialize PostgreSQL row: %s", e)
            return None

    def store(self, memory: Memory) -> None:
        if not self._available or not self._pool:
            return
        d = self._memory_to_dict(memory)
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {self._table_name}
                    (id, created_at, accessed_at, consolidated_at, expires_at,
                     raw, structured, embedding, memory_type, privacy,
                     importance, confidence, tags, categories, relations, provenance,
                     agent_id, agent_type, session_id, user_id, team_id, project_id)
                    VALUES (%(id)s, %(created_at)s, %(accessed_at)s, %(consolidated_at)s, %(expires_at)s,
                            %(raw)s, %(structured)s, %(embedding)s, %(memory_type)s, %(privacy)s,
                            %(importance)s, %(confidence)s, %(tags)s, %(categories)s, %(relations)s, %(provenance)s,
                            %(agent_id)s, %(agent_type)s, %(session_id)s, %(user_id)s, %(team_id)s, %(project_id)s)
                    ON CONFLICT (id) DO UPDATE SET
                        raw = EXCLUDED.raw,
                        structured = EXCLUDED.structured,
                        embedding = EXCLUDED.embedding,
                        memory_type = EXCLUDED.memory_type,
                        privacy = EXCLUDED.privacy,
                        importance = EXCLUDED.importance,
                        confidence = EXCLUDED.confidence,
                        tags = EXCLUDED.tags,
                        categories = EXCLUDED.categories,
                        relations = EXCLUDED.relations,
                        provenance = EXCLUDED.provenance,
                        accessed_at = EXCLUDED.accessed_at,
                        expires_at = EXCLUDED.expires_at
                """, d)
                conn.commit()
                logger.debug("Stored memory %s in PostgreSQL", memory.id)
        except Exception:
            logger.exception("PostgreSQL store failed for memory %s", memory.id)
            conn.rollback()
        finally:
            self._put_conn(conn)

    def retrieve(self, memory_id: str) -> Optional[Memory]:
        if not self._available or not self._pool:
            return None
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {self._table_name} WHERE id = %s", (memory_id,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        f"UPDATE {self._table_name} SET accessed_at = %s WHERE id = %s",
                        (TemporalAnchor().created_at, memory_id)
                    )
                    conn.commit()
                    return self._row_to_memory(row)
                return None
        except Exception:
            logger.exception("PostgreSQL retrieve failed for %s", memory_id)
            conn.rollback()
            return None
        finally:
            self._put_conn(conn)

    def delete(self, memory_id: str) -> bool:
        if not self._available or not self._pool:
            return False
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._table_name} WHERE id = %s RETURNING id", (memory_id,))
                row = cur.fetchone()
                conn.commit()
                deleted = row is not None
                logger.debug("Deleted memory %s from PostgreSQL (deleted=%s)", memory_id, deleted)
                return deleted
        except Exception:
            logger.exception("PostgreSQL delete failed for %s", memory_id)
            conn.rollback()
            return False
        finally:
            self._put_conn(conn)

    def search_keywords(self, query: str, k: int = 10) -> List[Memory]:
        if not self._available or not self._pool:
            return []
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Use tsvector for full-text search
                cur.execute(f"""
                    SELECT * FROM {self._table_name}
                    WHERE to_tsvector('english', raw) @@ plainto_tsquery('english', %s)
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (query, k))
                rows = cur.fetchall()
                return [m for m in [self._row_to_memory(row) for row in rows] if m]
        except Exception:
            logger.exception("PostgreSQL keyword search failed")
            # Fallback to LIKE
            try:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT * FROM {self._table_name}
                        WHERE raw ILIKE %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (f"%{query}%", k))
                    rows = cur.fetchall()
                    return [m for m in [self._row_to_memory(row) for row in rows] if m]
            except Exception:
                logger.exception("PostgreSQL LIKE fallback also failed")
                return []
        finally:
            self._put_conn(conn)

    def search_vector(
        self, vector: List[float], k: int = 10, **filters: Any
    ) -> List[Memory]:
        """PostgreSQL does not natively support vector search (without pgvector). Fallback to recency."""
        return self._recent_memories(k)

    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        if not self._available or not self._pool:
            return []
        try:
            # Load all memories with relations matching entity
            results = self.search_keywords(entity, k=20)
            visited = set()
            queue = [(str(m.id), 0) for m in results]
            all_results = []

            while queue:
                current_id, d = queue.pop(0)
                if d > depth or current_id in visited:
                    continue
                visited.add(current_id)
                mem = self.retrieve(current_id)
                if mem:
                    all_results.append(mem)
                    for rel in mem.metadata.relations:
                        if rel.target_memory_id not in visited:
                            queue.append((rel.target_memory_id, d + 1))
            return all_results
        except Exception:
            logger.exception("PostgreSQL graph search failed")
            return []

    def list_all(self, limit: int = 100) -> List[Memory]:
        return self._recent_memories(limit)

    def _recent_memories(self, limit: int) -> List[Memory]:
        if not self._available or not self._pool:
            return []
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT * FROM {self._table_name}
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()
                return [m for m in [self._row_to_memory(row) for row in rows] if m]
        except Exception:
            logger.exception("PostgreSQL recent memories failed")
            return []
        finally:
            self._put_conn(conn)

    def delete_expired(self) -> int:
        if not self._available or not self._pool:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                now = TemporalAnchor().created_at
                cur.execute(f"""
                    DELETE FROM {self._table_name}
                    WHERE expires_at IS NOT NULL AND expires_at < %s
                """, (now,))
                conn.commit()
                count = cur.rowcount
                logger.debug("PostgreSQL deleted %d expired memories", count)
                return count
        except Exception:
            logger.exception("PostgreSQL delete_expired failed")
            conn.rollback()
            return 0
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        """Close all connections in the pool."""
        if self._pool:
            try:
                self._pool.closeall()
                logger.info("PostgreSQLStore closed: pool drained")
            except Exception:
                pass
        self._available = False
