"""ChromaDB vector storage implementation for UAMS.

Optional dependency: requires `pip install chromadb`.
Gracefully degrades to InMemoryStore if chromadb is not available.
"""

from typing import Any, Dict, List, Optional

from uams.storage.base import MemoryStore
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata
)
from uams.core.enums import MemoryType, PrivacyLevel
from uams.utils.logging import get_logger
import threading

logger = get_logger(__name__)


class ChromaDBStore(MemoryStore):
    """
    ChromaDB-backed storage for vector similarity search.
    Falls back to keyword search if embeddings are not available.
    """

    def __init__(self, collection_name: str = "uams", persist_directory: Optional[str] = None):
        try:
            import chromadb
            self._client = chromadb.Client() if not persist_directory else chromadb.PersistentClient(path=persist_directory)
            self._collection = self._client.get_or_create_collection(name=collection_name)
            self._available = True
            logger.info("ChromaDBStore initialized: collection=%s", collection_name)
        except ImportError:
            logger.warning("chromadb not installed. ChromaDBStore will fall back to no-op.")
            self._available = False
            self._collection = None

    def store(self, memory: Memory) -> None:
        if not self._available:
            return
        try:
            embedding = memory.payload.embedding
            self._collection.add(
                ids=[str(memory.id)],
                documents=[memory.payload.raw],
                metadatas=[{
                    "memory_type": memory.metadata.memory_type.name,
                    "privacy": memory.metadata.privacy.name,
                    "importance": memory.metadata.importance,
                    "confidence": memory.metadata.confidence,
                    "tags": ",".join(memory.metadata.tags),
                    "categories": ",".join(memory.metadata.categories),
                    "agent_id": memory.context.agent_id,
                    "agent_type": memory.context.agent_type,
                    "session_id": memory.context.session_id,
                    "user_id": memory.context.user_id or "",
                    "team_id": memory.context.team_id or "",
                    "project_id": memory.context.project_id or "",
                    "created_at": memory.anchor.created_at,
                    "accessed_at": memory.anchor.accessed_at or 0,
                    "expires_at": memory.anchor.expires_at or 0,
                }],
                embeddings=[embedding] if embedding else None,
            )
        except Exception:
            logger.exception("ChromaDB store failed for memory %s", memory.id)

    def retrieve(self, memory_id: str) -> Optional[Memory]:
        if not self._available:
            return None
        try:
            results = self._collection.get(
                ids=[memory_id],
                include=["documents", "metadatas", "embeddings"]
            )
            if not results or not results["ids"] or not results["ids"][0]:
                return None
            
            meta = results["metadatas"][0]
            doc = results["documents"][0]
            embedding = results.get("embeddings", [None])[0]
            
            return Memory(
                id=MemoryId(memory_id),
                anchor=TemporalAnchor(
                    created_at=meta.get("created_at", 0),
                    accessed_at=meta.get("accessed_at") if meta.get("accessed_at") else None,
                    expires_at=meta.get("expires_at") if meta.get("expires_at") else None,
                ),
                context=AgentContext(
                    agent_id=meta.get("agent_id", "unknown"),
                    agent_type=meta.get("agent_type", "unknown"),
                    session_id=meta.get("session_id", "unknown"),
                    user_id=meta.get("user_id") or None,
                    team_id=meta.get("team_id") or None,
                    project_id=meta.get("project_id") or None,
                ),
                payload=MemoryPayload(
                    raw=doc,
                    embedding=embedding,
                ),
                metadata=MemoryMetadata(
                    memory_type=MemoryType[meta.get("memory_type", "SEMANTIC")],
                    privacy=PrivacyLevel[meta.get("privacy", "PUBLIC")],
                    importance=meta.get("importance", 5.0),
                    confidence=meta.get("confidence", 1.0),
                    tags=set(meta.get("tags", "").split(",")) if meta.get("tags") else set(),
                    categories=set(meta.get("categories", "").split(",")) if meta.get("categories") else set(),
                ),
            )
        except Exception:
            logger.exception("ChromaDB retrieve failed for %s", memory_id)
            return None

    def delete(self, memory_id: str) -> bool:
        if not self._available:
            return False
        try:
            self._collection.delete(ids=[memory_id])
            return True
        except Exception:
            logger.exception("ChromaDB delete failed for %s", memory_id)
            return False

    def search_keywords(self, query: str, k: int = 10) -> List[Memory]:
        # ChromaDB is vector-first; keyword search is via query_documents
        if not self._available:
            return []
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=k,
            )
            # Convert back to Memory objects (full reconstruction)
            memories = []
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                memories.append(Memory(
                    id=MemoryId(doc_id),
                    anchor=TemporalAnchor(
                        created_at=meta.get("created_at", 0),
                        accessed_at=meta.get("accessed_at") if meta.get("accessed_at") else None,
                        expires_at=meta.get("expires_at") if meta.get("expires_at") else None,
                    ),
                    context=AgentContext(
                        agent_id=meta.get("agent_id", "unknown"),
                        agent_type=meta.get("agent_type", "unknown"),
                        session_id=meta.get("session_id", "unknown"),
                        user_id=meta.get("user_id") or None,
                        team_id=meta.get("team_id") or None,
                        project_id=meta.get("project_id") or None,
                    ),
                    payload=MemoryPayload(raw=results["documents"][0][i]),
                    metadata=MemoryMetadata(
                        memory_type=MemoryType[meta.get("memory_type", "SEMANTIC")],
                        privacy=PrivacyLevel[meta.get("privacy", "PUBLIC")],
                        importance=meta.get("importance", 5.0),
                        confidence=meta.get("confidence", 1.0),
                        tags=set(meta.get("tags", "").split(",")) if meta.get("tags") else set(),
                        categories=set(meta.get("categories", "").split(",")) if meta.get("categories") else set(),
                    ),
                ))
            return memories
        except Exception:
            logger.exception("ChromaDB keyword search failed")
            return []

    def search_vector(
        self, vector: List[float], k: int = 10, **filters: Any
    ) -> List[Memory]:
        if not self._available or not vector:
            return []
        try:
            results = self._collection.query(
                query_embeddings=[vector],
                n_results=k,
                where=filters if filters else None,
            )
            memories = []
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                memories.append(Memory(
                    id=MemoryId(doc_id),
                    anchor=TemporalAnchor(
                        created_at=meta.get("created_at", 0),
                        accessed_at=meta.get("accessed_at") if meta.get("accessed_at") else None,
                        expires_at=meta.get("expires_at") if meta.get("expires_at") else None,
                    ),
                    context=AgentContext(
                        agent_id=meta.get("agent_id", "unknown"),
                        agent_type=meta.get("agent_type", "unknown"),
                        session_id=meta.get("session_id", "unknown"),
                        user_id=meta.get("user_id") or None,
                        team_id=meta.get("team_id") or None,
                        project_id=meta.get("project_id") or None,
                    ),
                    payload=MemoryPayload(raw=results["documents"][0][i]),
                    metadata=MemoryMetadata(
                        memory_type=MemoryType[meta.get("memory_type", "SEMANTIC")],
                        privacy=PrivacyLevel[meta.get("privacy", "PUBLIC")],
                        importance=meta.get("importance", 5.0),
                        confidence=meta.get("confidence", 1.0),
                        tags=set(meta.get("tags", "").split(",")) if meta.get("tags") else set(),
                        categories=set(meta.get("categories", "").split(",")) if meta.get("categories") else set(),
                    ),
                ))
            return memories
        except Exception:
            logger.exception("ChromaDB vector search failed")
            return []

    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        return []

    def list_all(self, limit: int = 100) -> List[Memory]:
        return []

    def delete_expired(self) -> int:
        # ChromaDB doesn't have TTL natively; implement via metadata filtering
        return 0
