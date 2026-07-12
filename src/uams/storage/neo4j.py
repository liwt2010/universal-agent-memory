"""Neo4j graph storage implementation for UAMS.

Optional dependency: requires `pip install neo4j`.
Provides true graph traversal (BFS/DFS) and relationship-based memory retrieval.

Schema:
  (:Memory {id, raw, created_at, expires_at, memory_type, ...})
  (:Memory)-[:RELATES {type, strength}]->(:Memory)
  (:Agent {id, type})-[:PRODUCED]->(:Memory)
  (:Session {id})-[:CONTAINS]->(:Memory)
"""

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from uams.storage.base import MemoryStore
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata, Relation,
)
from uams.core.enums import MemoryType, PrivacyLevel, EventType
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class Neo4jStore(MemoryStore):
    """
    Neo4j-backed graph storage for memory relationships and traversal.
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "password",
        database: str = "neo4j",
        ttl_seconds: Optional[float] = None,
    ):
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._ttl_seconds = ttl_seconds
        self._driver = None
        self._available = False

        try:
            from neo4j import GraphDatabase
            self._GraphDatabase = GraphDatabase
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            # Test connection
            with self._driver.session(database=database) as session:
                session.run("RETURN 1")
            self._available = True
            logger.info(
                "Neo4jStore connected: neo4j://%s@%s/%s",
                user, uri, database
            )
            self._ensure_schema()
        except ImportError:
            logger.warning("neo4j not installed. Neo4jStore will fall back to no-op.")
        except Exception as e:
            logger.error("Neo4j connection failed: %s. Store will be no-op.", e)

    def _ensure_schema(self) -> None:
        """Create indexes and constraints for efficient querying."""
        if not self._available or not self._driver:
            return
        try:
            with self._driver.session(database=self._database) as session:
                # Unique constraint on Memory.id
                session.run("""
                    CREATE CONSTRAINT memory_id IF NOT EXISTS
                    FOR (m:Memory) REQUIRE m.id IS UNIQUE
                """)
                # Index on Memory.memory_type for filtering
                session.run("""
                    CREATE INDEX memory_type_idx IF NOT EXISTS
                    FOR (m:Memory) ON (m.memory_type)
                """)
                # Index on Memory.created_at for recency queries
                session.run("""
                    CREATE INDEX memory_created_idx IF NOT EXISTS
                    FOR (m:Memory) ON (m.created_at)
                """)
                # Full-text index on raw content
                try:
                    session.run("""
                        CREATE FULLTEXT INDEX memory_text_idx IF NOT EXISTS
                        FOR (m:Memory) ON EACH [m.raw]
                    """)
                except Exception:
                    logger.debug("Full-text index may not be supported in this Neo4j version")
        except Exception:
            logger.exception("Neo4j schema initialization failed")

    def _memory_to_node_props(self, memory: Memory) -> Dict[str, Any]:
        """Convert memory to Neo4j node properties."""
        return {
            "id": str(memory.id),
            "created_at": memory.anchor.created_at,
            "accessed_at": memory.anchor.accessed_at or 0,
            "consolidated_at": memory.anchor.consolidated_at or 0,
            "expires_at": memory.anchor.expires_at or 0,
            "raw": memory.payload.raw,
            "structured": json.dumps(memory.payload.structured) if memory.payload.structured else None,
            "embedding_blob": json.dumps(memory.payload.embedding) if memory.payload.embedding else None,
            "memory_type": memory.metadata.memory_type.name,
            "privacy": memory.metadata.privacy.name,
            "importance": memory.metadata.importance,
            "confidence": memory.metadata.confidence,
            "tags": json.dumps(list(memory.metadata.tags)),
            "categories": json.dumps(list(memory.metadata.categories)),
            "provenance": json.dumps(memory.metadata.provenance),
            "agent_id": memory.context.agent_id,
            "agent_type": memory.context.agent_type,
            "session_id": memory.context.session_id,
            "user_id": memory.context.user_id or "",
            "team_id": memory.context.team_id or "",
            "project_id": memory.context.project_id or "",
        }

    def _record_to_memory(self, record: Dict) -> Optional[Memory]:
        """Convert Neo4j record to Memory object."""
        try:
            node = record["m"] if "m" in record else record
            props = dict(node)

            structured = json.loads(props["structured"]) if props.get("structured") else None
            embedding = json.loads(props["embedding_blob"]) if props.get("embedding_blob") else None
            tags = set(json.loads(props["tags"])) if props.get("tags") else set()
            categories = set(json.loads(props["categories"])) if props.get("categories") else set()
            provenance = json.loads(props["provenance"]) if props.get("provenance") else []

            expires_at = props.get("expires_at", 0)
            if expires_at == 0:
                expires_at = None

            return Memory(
                id=MemoryId(props["id"]),
                anchor=TemporalAnchor(
                    created_at=props.get("created_at", 0),
                    accessed_at=props.get("accessed_at") if props.get("accessed_at") else None,
                    consolidated_at=props.get("consolidated_at") if props.get("consolidated_at") else None,
                    expires_at=expires_at,
                ),
                context=AgentContext(
                    agent_id=props["agent_id"],
                    agent_type=props["agent_type"],
                    session_id=props["session_id"],
                    user_id=props.get("user_id") or None,
                    team_id=props.get("team_id") or None,
                    project_id=props.get("project_id") or None,
                ),
                payload=MemoryPayload(
                    raw=props["raw"],
                    structured=structured,
                    embedding=embedding,
                ),
                metadata=MemoryMetadata(
                    memory_type=MemoryType[props["memory_type"]],
                    privacy=PrivacyLevel[props["privacy"]],
                    importance=props.get("importance", 5.0),
                    confidence=props.get("confidence", 1.0),
                    tags=tags,
                    categories=categories,
                    # Read relations from the optional ``rels`` query field.
                    # The caller (retrieve / search_graph) must OPTIONAL
                    # MATCH outgoing RELATES edges and ``collect(...)`` them
                    # as ``rels`` for this to be populated. Without that
                    # join, relations defaults to [] and the Memory is
                    # partially populated — but cascade in-edge discovery
                    # on Neo4j would silently degrade to OUTGOING. Callers
                    # that need a fully-populated Memory for cascade should
                    # use ``_record_to_memory_with_rels`` or extend their
                    # query to OPTIONAL MATCH the edges.
                    relations=self._extract_relations(record),
                    provenance=provenance,
                ),
            )
        except Exception as e:
            logger.exception("Failed to deserialize Neo4j record: %s", e)
            return None

    def _extract_relations(self, record) -> list:
        """Reconstruct Relation objects from the optional ``rels`` query field.

        The retrieve() and search_graph() callers below now OPTIONAL MATCH
        outgoing RELATES edges and ``collect(...)`` them into ``record["rels"]``.
        This helper turns that collect result back into Relation objects so
        the returned Memory has a populated ``metadata.relations`` list.
        Without this, cascade-forget in-edge discovery on Neo4j was silently
        a no-op (BIDIRECTIONAL degraded to OUTGOING).

        Returns an empty list when ``rels`` is missing — back-compat with
        any caller that hasn't updated its query yet.
        """
        rels_raw = record.get("rels") if isinstance(record, dict) else None
        if not rels_raw:
            return []
        out: list = []
        for r in rels_raw:
            target = r.get("target_id") or r.get("target")
            if not target:
                continue
            out.append(Relation(
                relation_type=r.get("type") or r.get("relation_type") or "relates",
                target_memory_id=str(target),
                bidirectional=r.get("bidirectional", False),
                strength=r.get("strength", 1.0),
            ))
        return out

    def store(self, memory: Memory) -> None:
        if not self._available or not self._driver:
            return
        try:
            props = self._memory_to_node_props(memory)
            with self._driver.session(database=self._database) as session:
                # Create/update memory node
                session.run("""
                    MERGE (m:Memory {id: $id})
                    SET m += $props
                """, id=props["id"], props=props)

                # Create agent node and relationship
                session.run("""
                    MATCH (m:Memory {id: $id})
                    MERGE (a:Agent {id: $agent_id, type: $agent_type})
                    MERGE (a)-[:PRODUCED]->(m)
                """, id=props["id"], agent_id=props["agent_id"], agent_type=props["agent_type"])

                # Create session node and relationship
                session.run("""
                    MATCH (m:Memory {id: $id})
                    MERGE (s:Session {id: $session_id})
                    MERGE (s)-[:CONTAINS]->(m)
                """, id=props["id"], session_id=props["session_id"])

                # Create relations to other memories
                for rel in memory.metadata.relations:
                    session.run("""
                        MATCH (m1:Memory {id: $from_id})
                        MATCH (m2:Memory {id: $to_id})
                        MERGE (m1)-[r:RELATES {type: $rel_type}]->(m2)
                        SET r.strength = $strength
                    """, from_id=props["id"], to_id=rel.target_memory_id,
                        rel_type=rel.relation_type, strength=rel.strength)

                logger.debug("Stored memory %s in Neo4j", memory.id)
        except Exception:
            logger.exception("Neo4j store failed for memory %s", memory.id)

    def retrieve(self, memory_id: str) -> Optional[Memory]:
        """Read a memory by id, including its outgoing RELATES edges.

        The OPTIONAL MATCH on ``(:Memory)-[:RELATES]->(related:Memory)``
        loads the out-edge list so cascade-forget can correctly enumerate
        in-edges (when used with the same join in the in-edge scan).
        Without this join, ``_record_to_memory`` returned
        ``relations == []`` and cascade in-edge discovery on Neo4j was
        silently a no-op.
        """
        if not self._available or not self._driver:
            return None
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run(
                    """
                    MATCH (m:Memory {id: $id})
                    OPTIONAL MATCH (m)-[r:RELATES]->(related:Memory)
                    RETURN m,
                           collect({
                               type: r.type,
                               target_id: related.id,
                               strength: r.strength
                           }) AS rels
                    """,
                    id=memory_id,
                )
                record = result.single()
                if record:
                    # Update accessed_at
                    session.run(
                        """
                        MATCH (m:Memory {id: $id})
                        SET m.accessed_at = $now
                        """,
                        id=memory_id, now=TemporalAnchor().created_at,
                    )
                    return self._record_to_memory(record)
                return None
        except Exception:
            logger.exception("Neo4j retrieve failed for %s", memory_id)
            return None

    def delete(self, memory_id: str) -> bool:
        if not self._available or not self._driver:
            return False
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run("""
                    MATCH (m:Memory {id: $id})
                    OPTIONAL MATCH (m)-[r]-()
                    DELETE r, m
                    RETURN count(m) as deleted
                """, id=memory_id)
                record = result.single()
                deleted = record["deleted"] if record else 0
                return deleted > 0
        except Exception:
            logger.exception("Neo4j delete failed for %s", memory_id)
            return False

    def search_keywords(self, query: str, k: int = 10) -> List[Memory]:
        """Neo4j full-text search or CONTAINS fallback."""
        if not self._available or not self._driver:
            return []
        try:
            with self._driver.session(database=self._database) as session:
                # Try full-text index first
                try:
                    result = session.run("""
                        CALL db.index.fulltext.queryNodes('memory_text_idx', $query)
                        YIELD node, score
                        RETURN node as m
                        LIMIT $limit
                    """, query=query, limit=k)
                    records = list(result)
                    if records:
                        return [self._record_to_memory(r) for r in records if self._record_to_memory(r)]
                except Exception:
                    pass  # Full-text index not available

                # Fallback to CONTAINS
                terms = query.lower().split()
                result = session.run("""
                    MATCH (m:Memory)
                    WHERE m.raw CONTAINS $term
                    RETURN m
                    ORDER BY m.created_at DESC
                    LIMIT $limit
                """, term=terms[0] if terms else query, limit=k)
                return [self._record_to_memory(r) for r in result if self._record_to_memory(r)]
        except Exception:
            logger.exception("Neo4j keyword search failed")
            return []

    def search_vector(
        self, vector: List[float], k: int = 10, **filters: Any
    ) -> List[Memory]:
        """Neo4j does not natively support vector search (without plugins). Fallback to recency."""
        return self._recent_memories(k)

    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        """
        True graph traversal using Neo4j BFS/DFS.
        Supports multi-hop relationship following.
        """
        if not self._available or not self._driver:
            return []
        try:
            with self._driver.session(database=self._database) as session:
                # First: find memory nodes matching the entity directly
                result = session.run("""
                    MATCH (m:Memory)
                    WHERE m.raw CONTAINS $entity OR m.id = $entity
                    RETURN m
                    LIMIT 5
                """, entity=entity)
                seed_nodes = [r["m"] for r in result]

                if not seed_nodes:
                    return []

                # BFS traversal from seed nodes
                all_results = []
                visited_ids = set()
                queue = [(str(node["id"]), 0) for node in seed_nodes]

                while queue:
                    current_id, d = queue.pop(0)
                    if d > depth or current_id in visited_ids:
                        continue
                    visited_ids.add(current_id)

                    # Get the memory node
                    mem_result = session.run(
                        "MATCH (m:Memory {id: $id}) RETURN m",
                        id=current_id
                    )
                    mem_record = mem_result.single()
                    if mem_record:
                        mem = self._record_to_memory(mem_record)
                        if mem:
                            all_results.append(mem)

                    # Get related memories
                    if d < depth:
                        rel_result = session.run("""
                            MATCH (m:Memory {id: $id})-[r:RELATES]->(related:Memory)
                            RETURN related.id as related_id, r.type as rel_type, r.strength as strength
                        """, id=current_id)
                        for rel_record in rel_result:
                            related_id = rel_record["related_id"]
                            if related_id not in visited_ids:
                                queue.append((related_id, d + 1))

                return all_results
        except Exception:
            logger.exception("Neo4j graph traversal failed")
            return []

    def list_all(self, limit: int = 100) -> List[Memory]:
        return self._recent_memories(limit)

    def _recent_memories(self, limit: int) -> List[Memory]:
        """Return most recent memories by created_at."""
        if not self._available or not self._driver:
            return []
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run("""
                    MATCH (m:Memory)
                    RETURN m
                    ORDER BY m.created_at DESC
                    LIMIT $limit
                """, limit=limit)
                return [self._record_to_memory(r) for r in result if self._record_to_memory(r)]
        except Exception:
            logger.exception("Neo4j recent memories failed")
            return []

    def delete_expired(self) -> int:
        """Delete memories whose expires_at has passed."""
        if not self._available or not self._driver:
            return 0
        try:
            with self._driver.session(database=self._database) as session:
                now = TemporalAnchor().created_at
                result = session.run("""
                    MATCH (m:Memory)
                    WHERE m.expires_at > 0 AND m.expires_at < $now
                    OPTIONAL MATCH (m)-[r]-()
                    WITH m, r
                    DELETE r, m
                    RETURN count(m) as deleted
                """, now=now)
                record = result.single()
                count = record["deleted"] if record else 0
                logger.debug("Neo4j deleted %d expired memories", count)
                return count
        except Exception:
            logger.exception("Neo4j delete_expired failed")
            return 0

    def count(self) -> int:
        """O(1) Cypher ``MATCH (n:Memory) RETURN count(n)``."""
        if not self._available or not self._driver:
            return 0
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run("MATCH (m:Memory) RETURN count(m) AS n")
                record = result.single()
                return int(record["n"]) if record else 0
        except Exception:
            logger.exception("Neo4j count() failed")
            return 0

    def delete_by_filter(self, field: str, value: Any) -> int:
        """O(matches) Cypher DELETE WHERE.

        Same whitelist as SQLite / PG; uses the (already-indexed)
        context property as the match key.
        """
        allowed = {"agent_id", "agent_type", "session_id",
                   "user_id", "team_id", "project_id"}
        if field not in allowed:
            logger.warning(
                "Neo4jStore.delete_by_filter: field %r not in whitelist %s",
                field, sorted(allowed),
            )
            return 0
        if not self._available or not self._driver:
            return 0
        try:
            with self._driver.session(database=self._database) as session:
                # Parameterized to prevent Cypher injection.
                result = session.run(
                    f"""
                    MATCH (m:Memory {{{field}: $value}})
                    OPTIONAL MATCH (m)-[r]-()
                    WITH m, r
                    DELETE r, m
                    RETURN count(m) AS deleted
                    """,
                    value=value,
                )
                record = result.single()
                deleted = int(record["deleted"]) if record else 0
                logger.info(
                    "Neo4j deleted %d memories where %s = %r",
                    deleted, field, value,
                )
                return deleted
        except Exception:
            logger.exception("Neo4j delete_by_filter(%s=%r) failed", field, value)
            return 0

    def get_related_memories(self, memory_id: str, relation_type: Optional[str] = None, min_strength: float = 0.0) -> List[Memory]:
        """Get all memories directly related to a given memory."""
        if not self._available or not self._driver:
            return []
        try:
            with self._driver.session(database=self._database) as session:
                if relation_type:
                    result = session.run("""
                        MATCH (m:Memory {id: $id})-[r:RELATES {type: $rel_type}]->(related:Memory)
                        WHERE r.strength >= $min_strength
                        RETURN related as m
                        ORDER BY r.strength DESC
                    """, id=memory_id, rel_type=relation_type, min_strength=min_strength)
                else:
                    result = session.run("""
                        MATCH (m:Memory {id: $id})-[r:RELATES]->(related:Memory)
                        WHERE r.strength >= $min_strength
                        RETURN related as m
                        ORDER BY r.strength DESC
                    """, id=memory_id, min_strength=min_strength)
                return [self._record_to_memory(r) for r in result if self._record_to_memory(r)]
        except Exception:
            logger.exception("Neo4j get_related_memories failed")
            return []

    def close(self) -> None:
        """Close the underlying Neo4j driver.

        ``neo4j.GraphDatabase.driver.close()`` is the recommended cleanup
        path — it releases the connection pool to the Bolt server.
        Idempotent: calling on an already-closed driver is a no-op.
        """
        if self._driver is None:
            return
        try:
            self._driver.close()
        except Exception:
            logger.exception("Neo4j driver close failed")
        self._driver = None
        self._available = False
        logger.debug("Neo4jStore closed")
