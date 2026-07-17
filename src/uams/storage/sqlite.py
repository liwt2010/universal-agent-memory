"""SQLite persistent storage implementation for UAMS.

Supports full CRUD, FTS5 full-text search, vector blob storage,
and thread-safe access via RLock + WAL mode + connection pool.

All write operations are atomic (BEGIN/COMMIT/ROLLBACK).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from queue import Queue
from typing import Any

from uams.storage.base import MemoryStore
from uams.core.models import Memory, MemoryId, TemporalAnchor, AgentContext, MemoryPayload, MemoryMetadata, Relation
from uams.core.enums import MemoryType, PrivacyLevel
from uams.utils.logging import get_logger
from uams.utils.embedding_serde import serialize_embedding, deserialize_embedding

logger = get_logger(__name__)

_SCHEMA_VERSION = 2  # v2: add tenant_id column for multi-tenant GDPR

# SQLite default SQLITE_MAX_VARIABLE_NUMBER (the cap on bound parameters
# in a single prepared statement). Values passed to LIMIT clauses via
# parameter binding must not exceed this — see list_all() for the
# clamping logic. This constant is also a runtime sanity check for the
# test suite.
_SQLITE_MAX_VARIABLE_NUMBER = 999


class SQLiteStore(MemoryStore):
    """
    SQLite-backed persistent memory store with WAL mode for concurrent reads.
    Supports full-text search via FTS5 virtual table.
    Uses connection pool to avoid creating connections per operation.
    All writes are atomic transactions.
    """

    def __init__(self, db_path: str = "uams.db", tier_name: str = "memory", pool_size: int = 8):
        # Default pool_size=8 to support 1 serialized writer + multiple concurrent readers
        # (WAL mode serializes writes, so 5 was tight under 4+ concurrent write threads).
        self._db_path = db_path
        self._tier_name = tier_name
        self._pool_size = pool_size
        self._lock = threading.RLock()
        self._pool: Queue[sqlite3.Connection] = Queue(maxsize=pool_size)
        self._available = True
        # Track every connection ever handed out so close() can also
        # close connections held by in-flight threads (which the pool
        # itself can't see — they're between _get_connection() and
        # _return_connection() at the moment close() runs).
        self._all_conns: set[sqlite3.Connection] = set()

        # Initialize pool connections
        for _ in range(pool_size):
            conn = self._create_connection()
            self._all_conns.add(conn)
            self._pool.put(conn)

        self._ensure_schema()
        self._run_migrations()
        logger.info("SQLiteStore initialized: db=%s tier=%s pool=%d", db_path, tier_name, pool_size)

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # busy_timeout=5000: SQLite retries up to 5s on SQLITE_BUSY before raising.
        # Belt-and-suspenders alongside write-side RLock below.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        # Refuse to hand out connections after close() so background writes
        # don't block forever on Queue.get() of an empty pool. close()
        # already drains the queue and sets _available=False; this guard
        # makes the failure mode a clear RuntimeError instead of either
        # a hang (when the queue happens to still have an item) or a
        # confusing OperationalError (when Queue.get() returns a closed
        # connection from _all_conns).
        if not self._available:
            raise RuntimeError(
                f"SQLiteStore(tier={self._tier_name}) is closed; "
                "no further operations are permitted"
            )
        return self._pool.get()

    def _return_connection(self, conn: sqlite3.Connection) -> None:
        # If close() already ran, this connection is being returned by
        # an in-flight thread that started before shutdown. Close it
        # here so it doesn't leak — and don't put it back in the pool,
        # which is being drained.
        if not self._available:
            try:
                conn.close()
            except Exception:
                pass
            return
        try:
            # Only ROLLBACK if the connection is mid-transaction.
            # ``conn.in_transaction`` is True iff a BEGIN has been
            # issued and no COMMIT/ROLLBACK has closed it. The
            # schema-upgrade path in _ensure_schema commits and then
            # issues DDL (ALTER / CREATE INDEX) on the same
            # connection; if we then unconditionally ROLLBACK on
            # return-to-pool, the schema changes silently disappear.
            # in_transaction check keeps the old "always rollback"
            # safety net (so a buggy writer that forgot to commit
            # doesn't leak an open transaction) while letting clean
            # paths keep their commit.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except Exception:
            pass
        try:
            self._pool.put_nowait(conn)
        except Exception:
            # Pool is full / closed; close the connection rather than
            # silently drop it.
            try:
                conn.close()
            except Exception:
                pass

    def _ensure_schema(self) -> None:
        conn = self._get_connection()
        try:
            # Schema version tracking table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS _schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL
                )
            """)

            # Main memories table
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._tier_name}_memories (
                    id TEXT PRIMARY KEY,
                    created_at REAL,
                    accessed_at REAL,
                    consolidated_at REAL,
                    expires_at REAL,
                    raw TEXT NOT NULL,
                    structured TEXT,
                    embedding BLOB,
                    memory_type TEXT,
                    privacy TEXT,
                    importance REAL,
                    confidence REAL,
                    tags TEXT,
                    categories TEXT,
                    relations TEXT,
                    provenance TEXT,
                    agent_id TEXT,
                    agent_type TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    team_id TEXT,
                    project_id TEXT,
                    tenant_id TEXT
                )
            """)

            # Indexes for common queries
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._tier_name}_agent
                ON {self._tier_name}_memories(agent_id)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._tier_name}_session
                ON {self._tier_name}_memories(session_id)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._tier_name}_expires
                ON {self._tier_name}_memories(expires_at)
            """)

            # FTS5 virtual table for full-text search
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {self._tier_name}_fts USING fts5(
                    raw, id, content='{self._tier_name}_memories', content_rowid='rowid'
                )
            """)

            # Triggers to keep FTS5 in sync
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS {self._tier_name}_insert_fts
                AFTER INSERT ON {self._tier_name}_memories
                BEGIN
                    INSERT INTO {self._tier_name}_fts (rowid, raw, id)
                    VALUES (new.rowid, new.raw, new.id);
                END
            """)
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS {self._tier_name}_delete_fts
                AFTER DELETE ON {self._tier_name}_memories
                BEGIN
                    INSERT INTO {self._tier_name}_fts ({self._tier_name}_fts, rowid, id)
                    VALUES ('delete', old.rowid, old.id);
                END
            """)

            # Idempotent column add for upgrades from pre-v0.6.0 DBs
            # where the table predates the tenant_id column. Run the
            # PRAGMA detector on a fresh connection because DDL on
            # `conn` (CREATE TABLE + FTS5 above) is still inside a
            # transaction — table_info on `conn` would either be
            # empty or report the old 22-column shape, and any ALTER
            # we issued here would silently roll back when `conn`
            # later commits. Commit DDL first, then detect.
            conn.commit()

            pragma_conn = sqlite3.connect(self._db_path)
            try:
                pragma_conn.execute("PRAGMA journal_mode=WAL")
                pragma_conn.execute("PRAGMA busy_timeout=5000")
                cols = [
                    row[1]
                    for row in pragma_conn.execute(
                        f"PRAGMA table_info({self._tier_name}_memories)"
                    ).fetchall()
                ]
            finally:
                pragma_conn.close()

            if cols and "tenant_id" not in cols:
                logger.info(
                    "Upgrading %s_memories to add tenant_id column",
                    self._tier_name,
                )
                conn.execute(
                    f"ALTER TABLE {self._tier_name}_memories "
                    "ADD COLUMN tenant_id TEXT"
                )
            if cols:
                conn.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self._tier_name}_tenant
                    ON {self._tier_name}_memories(tenant_id)
                """)

            conn.commit()
        finally:
            self._return_connection(conn)

    def _run_migrations(self) -> None:
        """Apply schema migrations if needed."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("SELECT MAX(version) FROM _schema_version")
            row = cursor.fetchone()
            current_version = row[0] if row and row[0] is not None else 0

            if current_version < _SCHEMA_VERSION:
                logger.info("Migrating schema from %d to %d", current_version, _SCHEMA_VERSION)
                for v in range(current_version + 1, _SCHEMA_VERSION + 1):
                    self._apply_migration(conn, v)
                conn.execute(
                    "INSERT INTO _schema_version (version, applied_at) VALUES (?, ?)",
                    (_SCHEMA_VERSION, TemporalAnchor().created_at)
                )
                conn.commit()
                logger.info("Schema migration completed to version %d", _SCHEMA_VERSION)
        except Exception:
            logger.exception("Schema migration failed")
        finally:
            self._return_connection(conn)

    def _apply_migration(self, conn: sqlite3.Connection, version: int) -> None:
        """Apply a specific migration. Override this method for custom migrations."""
        if version == 1:
            # Initial schema - already created by _ensure_schema
            pass
        if version == 2:
            # Add tenant_id column for multi-tenant GDPR deletion
            # (P0-1 from the v0.5.2 audit). ALTER TABLE is idempotent
            # against newer tables that already have the column; we
            # catch the duplicate-column error so re-running migrations
            # is safe.
            try:
                conn.execute(
                    f"ALTER TABLE {self._tier_name}_memories "
                    "ADD COLUMN tenant_id TEXT"
                )
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
            # Add tenant index if missing (CREATE INDEX IF NOT EXISTS
            # is the standard idempotent form).
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._tier_name}_tenant
                ON {self._tier_name}_memories(tenant_id)
            """)
        logger.info("Applied migration version %d", version)

    def _memory_to_row(self, memory: Memory) -> tuple:
        return (
            str(memory.id),
            memory.anchor.created_at,
            memory.anchor.accessed_at,
            memory.anchor.consolidated_at,
            memory.anchor.expires_at,
            memory.payload.raw,
            json.dumps(memory.payload.structured) if memory.payload.structured else None,
            sqlite3.Binary(serialize_embedding(memory.payload.embedding)) if memory.payload.embedding else None,
            memory.metadata.memory_type.name,
            memory.metadata.privacy.name,
            memory.metadata.importance,
            memory.metadata.confidence,
            json.dumps(list(memory.metadata.tags)),
            json.dumps(list(memory.metadata.categories)),
            json.dumps([{"type": r.relation_type, "target": r.target_memory_id, "strength": r.strength} for r in memory.metadata.relations]),
            json.dumps(memory.metadata.provenance),
            memory.context.agent_id,
            memory.context.agent_type,
            memory.context.session_id,
            memory.context.user_id,
            memory.context.team_id,
            memory.context.project_id,
            memory.context.tenant_id,
        )

    def _row_to_memory(self, row: tuple) -> Memory:
        (
            id_str, created_at, accessed_at, consolidated_at, expires_at,
            raw, structured_str, embedding_blob, mem_type, privacy,
            importance, confidence, tags_str, categories_str, relations_str,
            provenance_str, agent_id, agent_type, session_id, user_id,
            team_id, project_id,
            tenant_id,
        ) = row

        structured = json.loads(structured_str) if structured_str else None
        embedding = deserialize_embedding(embedding_blob)
        tags = set(json.loads(tags_str)) if tags_str else set()
        categories = set(json.loads(categories_str)) if categories_str else set()
        relations = [Relation(
            r["type"], r["target"], strength=r.get("strength", 1.0)
        ) for r in json.loads(relations_str)] if relations_str else []
        provenance = json.loads(provenance_str) if provenance_str else []

        return Memory(
            id=MemoryId(id_str),
            anchor=TemporalAnchor(
                created_at=created_at,
                accessed_at=accessed_at,
                consolidated_at=consolidated_at,
                expires_at=expires_at,
            ),
            context=AgentContext(
                agent_id=agent_id,
                agent_type=agent_type,
                session_id=session_id,
                user_id=user_id,
                team_id=team_id,
                project_id=project_id,
                tenant_id=tenant_id,
            ),
            payload=MemoryPayload(
                raw=raw,
                structured=structured,
                embedding=embedding,
            ),
            metadata=MemoryMetadata(
                memory_type=MemoryType[mem_type],
                privacy=PrivacyLevel[privacy],
                importance=importance,
                confidence=confidence,
                tags=tags,
                categories=categories,
                relations=relations,
                provenance=provenance,
            ),
        )

    def store(self, memory: Memory) -> None:
        # Serialize writes: WAL mode serializes writes anyway, and without this
        # multiple writer threads see SQLITE_BUSY + busy_timeout retries. The
        # RLock turns "concurrent + slow retries" into "serialized + fast".
        # Reads (retrieve/search/list) stay concurrent — they use a different conn.
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN")
                conn.execute(f"""
                    INSERT OR REPLACE INTO {self._tier_name}_memories
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, self._memory_to_row(memory))
                conn.commit()
                logger.debug("SQLite stored memory %s in tier %s", memory.id, self._tier_name)
            except Exception:
                logger.exception("SQLite store failed for memory %s", memory.id)
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._return_connection(conn)

    def retrieve(self, memory_id: str) -> Memory | None:
        conn = self._get_connection()
        try:
            # The SELECT implicitly opens a read transaction in non-WAL
            # mode (Python's default sqlite3 isolation mode). In WAL mode
            # a pure SELECT does NOT open a transaction — `in_transaction`
            # stays False — so the redundant `BEGIN` that used to live
            # here was a latent footgun: in legacy journal mode it would
            # raise `OperationalError: cannot start a transaction within
            # a transaction` and the outer `except Exception` would
            # swallow it, turning every retrieve() hit into None.
            #
            # The fix removes the redundant BEGIN and reuses whatever
            # implicit transaction state already exists. This is safe in
            # both WAL and legacy journal modes.
            cursor = conn.execute(
                f"SELECT * FROM {self._tier_name}_memories WHERE id = ?",
                (memory_id,)
            )
            row = cursor.fetchone()
            if row:
                conn.execute(
                    f"UPDATE {self._tier_name}_memories SET accessed_at = ? WHERE id = ?",
                    (TemporalAnchor().created_at, memory_id)
                )
                conn.commit()
                return self._row_to_memory(row)
            return None
        except Exception:
            logger.exception("SQLite retrieve failed for %s", memory_id)
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        finally:
            self._return_connection(conn)

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN")
                cursor = conn.execute(
                    f"DELETE FROM {self._tier_name}_memories WHERE id = ?",
                    (memory_id,)
                )
                conn.commit()
                return cursor.rowcount > 0
            except Exception:
                logger.exception("SQLite delete failed for %s", memory_id)
                try:
                    conn.rollback()
                except Exception:
                    pass
                return False
            finally:
                self._return_connection(conn)

    def search_keywords(self, query: str, k: int = 10) -> list[Memory]:
        """FTS5 full-text search."""
        conn = self._get_connection()
        try:
            # FTS5 treats '-' as NOT operator and other characters as syntax.
            # We treat the user query as a literal phrase so 'state-of-the-art'
            # searches for the literal string, not 'state AND NOT of AND NOT ...'.
            fts_query = self._sanitize_fts5_query(query)
            cursor = conn.execute(f"""
                SELECT m.* FROM {self._tier_name}_memories m
                JOIN {self._tier_name}_fts f ON m.id = f.id
                WHERE {self._tier_name}_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, k))
            rows = cursor.fetchall()
            return [self._row_to_memory(row) for row in rows]
        except Exception:
            logger.exception("FTS5 search failed for query '%s'. Falling back to LIKE.", query)
            # Fallback to LIKE search
            try:
                cursor = conn.execute(f"""
                    SELECT * FROM {self._tier_name}_memories
                    WHERE raw LIKE ?
                    LIMIT ?
                """, (f"%{query}%", k))
                rows = cursor.fetchall()
                return [self._row_to_memory(row) for row in rows]
            except Exception:
                logger.exception("LIKE fallback also failed")
                return []
        finally:
            self._return_connection(conn)

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Wrap user query as an FTS5 phrase to avoid operator parsing.

        FTS5 syntax treats '-' as NOT, '*' as prefix, ':' as column filter, etc.
        A literal phrase ("...") tells FTS5 to match the exact token sequence,
        which is what users almost always want from `search_keywords()`.

        Embedded double quotes are escaped by doubling them (FTS5 convention).
        """
        if not query:
            return '""'
        escaped = query.replace('"', '""')
        return f'"{escaped}"'

    def search_vector(
        self, vector: list[float], k: int = 10, **filters: Any
    ) -> list[Memory]:
        """SQLite does not support vector search natively. Fallback to recency."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(f"""
                SELECT * FROM {self._tier_name}_memories
                ORDER BY created_at DESC
                LIMIT ?
            """, (k,))
            rows = cursor.fetchall()
            return [self._row_to_memory(row) for row in rows]
        except Exception:
            logger.exception("Vector search fallback failed")
            return []
        finally:
            self._return_connection(conn)

    def search_graph(self, entity: str, depth: int = 2) -> list[Memory]:
        """Graph traversal via relations JSON."""
        # This is expensive in SQLite. Return keyword match for now.
        return self.search_keywords(entity, k=10)

    def list_all(self, limit: int | None = 100) -> list[Memory]:
        """List memories ordered by created_at DESC.

        ``limit=None`` returns all memories in the tier (capped at a
        sane process-wide ceiling so a runaway table doesn't OOM). The
        ``limit`` value is clamped to SQLITE_MAX_VARIABLE_NUMBER (999)
        because ``list_all`` historically passed the value as a SQL
        parameter and ``sqlite3.OperationalError: too many SQL variables``
        on values >999 was silently swallowed by the outer ``except``,
        returning ``[]`` from callers like ``get_stats()``. Now we clamp
        to 999 so callers asking for "all" still get results.

        ``limit <= 0`` is treated as "use the default" (100) since 0 is
        rarely meaningful and almost always a caller bug.
        """
        if limit is None:
            effective_limit = _SQLITE_MAX_VARIABLE_NUMBER  # 999 — safe parameter
        elif limit <= 0:
            effective_limit = 100
        else:
            # Clamp to SQLite's parameter limit to avoid OperationalError.
            effective_limit = min(limit, _SQLITE_MAX_VARIABLE_NUMBER)
        conn = self._get_connection()
        try:
            cursor = conn.execute(f"""
                SELECT * FROM {self._tier_name}_memories
                ORDER BY created_at DESC
                LIMIT ?
            """, (effective_limit,))
            rows = cursor.fetchall()
            return [self._row_to_memory(row) for row in rows]
        except Exception:
            logger.exception("list_all failed")
            return []
        finally:
            self._return_connection(conn)

    def delete_expired(self) -> int:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN")
                now = TemporalAnchor().created_at
                cursor = conn.execute(f"""
                    DELETE FROM {self._tier_name}_memories
                    WHERE expires_at IS NOT NULL AND expires_at < ?
                """, (now,))
                conn.commit()
                count = cursor.rowcount
                logger.debug("Deleted %d expired memories from %s", count, self._tier_name)
                return count
            except Exception:
                logger.exception("delete_expired failed")
                try:
                    conn.rollback()
                except Exception:
                    pass
                return 0
            finally:
                self._return_connection(conn)

    def count(self) -> int:
        """O(1) round-trip COUNT(*) — replaces ``len(list_all(999999))``.

        Avoids the SQLITE_MAX_VARIABLE_NUMBER trap that bit the
        previous list_all()-based get_stats() implementation.
        """
        conn = self._get_connection()
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {self._tier_name}_memories"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            logger.exception("SQLite count() failed for tier %s", self._tier_name)
            return 0
        finally:
            self._return_connection(conn)

    def delete_by_filter(self, field: str, value: Any) -> int:
        """O(matches) delete via indexed WHERE on a flat context column.

        UAMS stores ``Memory.context`` as flat columns (``agent_id``,
        ``user_id``, ``project_id``, ...) rather than a single JSON
        blob, so the query is a direct column match. Only top-level
        context fields are supported (no dotted paths) — passing a
        nested path returns 0 with a warning.
        """
        # Whitelist: only top-level context fields are real columns.
        allowed = {"agent_id", "agent_type", "session_id",
                   "user_id", "team_id", "project_id", "tenant_id"}
        if field not in allowed:
            logger.warning(
                "SQLiteStore.delete_by_filter: field %r not in context "
                "whitelist %s; returning 0",
                field, sorted(allowed),
            )
            return 0
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    f"DELETE FROM {self._tier_name}_memories WHERE {field} = ?",
                    (value,),
                )
                conn.commit()
                deleted = cur.rowcount
                logger.info(
                    "SQLite deleted %d memories from %s where %s = %r",
                    deleted, self._tier_name, field, value,
                )
                return deleted
            except Exception:
                logger.exception(
                    "SQLite delete_by_filter(%s=%r) failed", field, value,
                )
                try:
                    conn.rollback()
                except Exception:
                    pass
                return 0
            finally:
                self._return_connection(conn)

    def delete_by_filters(
        self, filters: tuple[tuple[str, Any], ...]
    ) -> int:
        """O(matches) composite delete via single multi-predicate WHERE.

        All keys must be in the same flat-column whitelist as
        ``delete_by_filter`` (``agent_id``, ``agent_type``,
        ``session_id``, ``user_id``, ``team_id``, ``project_id``,
        ``tenant_id``). Anything outside the whitelist is rejected
        with a warning and the call returns 0.

        This replaces the previous O(rows) list_all round-trip used
        by ``delete_by_project_id(project_id, tenant_id=...)``, which
        silently dropped tenants past the 999-row list_all cap and
        blocked GDPR Article 17 deletion in any non-trivial
        deployment (P0-1 from the v0.5.2 audit).
        """
        allowed = {"agent_id", "agent_type", "session_id",
                   "user_id", "team_id", "project_id", "tenant_id"}
        bad = [f for f, _ in filters if f not in allowed]
        if bad:
            logger.warning(
                "SQLiteStore.delete_by_filters: fields %r not in context "
                "whitelist %s; returning 0",
                bad, sorted(allowed),
            )
            return 0
        if not filters:
            return 0
        with self._lock:
            conn = self._get_connection()
            try:
                where = " AND ".join(f"{f} = ?" for f, _ in filters)
                params = tuple(v for _, v in filters)
                cur = conn.execute(
                    f"DELETE FROM {self._tier_name}_memories WHERE {where}",
                    params,
                )
                conn.commit()
                deleted = cur.rowcount
                logger.info(
                    "SQLite deleted %d memories from %s where %s",
                    deleted, self._tier_name,
                    " AND ".join(f"{f}={v!r}" for f, v in filters),
                )
                return deleted
            except Exception:
                logger.exception(
                    "SQLite delete_by_filters(%s) failed", filters,
                )
                try:
                    conn.rollback()
                except Exception:
                    pass
                return 0
            finally:
                self._return_connection(conn)

    def close(self) -> None:
        """Close all connections in the pool.

        Closes connections sitting in the queue AND any that were
        checked out by in-flight threads at the moment close() ran —
        those connections are tracked via ``self._all_conns``. The
        _return_connection() helper checks ``self._available`` and
        closes the connection instead of putting it back, so post-close
        return-from-thread can't revive a dead pool.
        """
        with self._lock:
            # Drain the queue first.
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except Exception:
                    pass
            # Mark unavailable BEFORE closing in-flight connections —
            # _return_connection checks this flag to decide whether to
            # close-or-pool the returned conn.
            self._available = False
            # Close every connection ever created. After this point,
            # any thread holding a connection will get an error on its
            # next use; we accept that as the cost of a hard shutdown.
            for conn in list(self._all_conns):
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
            logger.info("SQLiteStore closed: db=%s tier=%s", self._db_path, self._tier_name)
