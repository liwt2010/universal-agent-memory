"""Core data models for the Universal Agent Memory System."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from uams.core.enums import MemoryType, EventType, PrivacyLevel


class MemoryId:
    """Globally unique memory identifier."""

    def __init__(self, id_str: str | None = None):
        self.id: str = id_str or str(uuid.uuid4())

    def __str__(self) -> str:
        return self.id

    def __repr__(self) -> str:
        return f"MemoryId({self.id[:8]}...)"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MemoryId):
            return self.id == other.id
        return False

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class TemporalAnchor:
    """Temporal metadata for a memory: creation, access, consolidation, expiry."""

    created_at: float = field(default_factory=time.time)
    accessed_at: float | None = None
    consolidated_at: float | None = None
    expires_at: float | None = None

    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def touch(self) -> None:
        self.accessed_at = time.time()


@dataclass
class AgentContext:
    """Context of the agent that produced this memory."""

    agent_id: str
    agent_type: str          # e.g. "personal_assistant", "game_npc", "researcher"
    session_id: str
    user_id: str | None = None
    team_id: str | None = None
    project_id: str | None = None
    tenant_id: str | None = None  # NEW: multi-tenant isolation boundary (cloud)

    def namespace(self) -> str:
        """Return a unique namespace for this memory owner.

        v0.6.0: includes ``tenant_id`` so multi-tenant deployments
        that share the same agent_id / user_id / team_id across
        tenants don't accidentally collide. tenant_id appears LAST
        in the join order so existing callers that read the first
        three parts don't see a behavioural change when tenant_id
        is None (it contributes an empty segment, which the join
        treats as '_').
        """
        parts = [
            self.agent_id,
            self.user_id or "_",
            self.team_id or "_",
            self.tenant_id or "_",
        ]
        return ":".join(parts)


@dataclass
class Relation:
    """Knowledge graph edge connecting memories."""

    relation_type: str               # e.g. "knows", "caused", "part_of", "prefers"
    target_memory_id: str
    bidirectional: bool = False
    strength: float = 1.0            # 0-1 edge weight


@dataclass
class MemoryPayload:
    """The actual content of a memory - completely domain-agnostic."""

    raw: str                          # Original raw observation
    structured: dict[str, Any] | None = None  # Extracted facts/entities
    embedding: list[float] | None = None      # Dense vector representation

    def fingerprint(self) -> str:
        """SHA-256 deduplication key."""
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()[:16]

    def to_search_doc(self) -> str:
        """Flatten for keyword indexing."""
        parts = [self.raw]
        if self.structured:
            parts.append(json.dumps(self.structured, ensure_ascii=False))
        return " ".join(parts)


@dataclass
class MemoryMetadata:
    """Searchable, filterable, and scorable attributes of a memory."""

    memory_type: MemoryType
    privacy: PrivacyLevel
    importance: float = 5.0          # 1-10, user or LLM assigned
    confidence: float = 1.0            # 0-1, certainty that this is true
    source_event: EventType | None = None
    tags: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    relations: list[Relation] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)  # Chain of derivation IDs

    def add_tag(self, tag: str) -> None:
        self.tags.add(tag)

    def add_category(self, category: str) -> None:
        self.categories.add(category)

    def add_relation(self, relation: Relation) -> None:
        self.relations.append(relation)


@dataclass
class Memory:
    """
    The universal memory unit.
    All domain specifics live in payload and metadata.
    System layers are completely domain-agnostic.
    """

    id: MemoryId
    anchor: TemporalAnchor
    context: AgentContext
    payload: MemoryPayload
    metadata: MemoryMetadata

    # Ephemeral scoring fields (not persisted)
    retrieval_score: float | None = None
    last_access_count: int = 0

    def touch(self) -> None:
        """Mark as accessed."""
        self.anchor.touch()
        self.last_access_count += 1

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Includes ``embedding`` (payload) and ``relations`` (metadata) so
        ``BackupManager.backup_to_file`` / ``restore_from_file`` roundtrip
        is lossless. The previous implementation omitted both fields,
        silently breaking vector search and cascade-forget after restore.
        """
        return {
            "id": str(self.id),
            "anchor": {
                "created_at": self.anchor.created_at,
                "accessed_at": self.anchor.accessed_at,
                "consolidated_at": self.anchor.consolidated_at,
                "expires_at": self.anchor.expires_at,
            },
            "context": {
                "agent_id": self.context.agent_id,
                "agent_type": self.context.agent_type,
                "session_id": self.context.session_id,
                "user_id": self.context.user_id,
                "team_id": self.context.team_id,
                "project_id": self.context.project_id,
                "tenant_id": self.context.tenant_id,
            },
            "payload": {
                "raw": self.payload.raw,
                "structured": self.payload.structured,
                # embedding is included so backup/restore preserves vector
                # search capability. None is serialized as JSON null.
                "embedding": self.payload.embedding,
            },
            "metadata": {
                "memory_type": self.metadata.memory_type.name,
                "privacy": self.metadata.privacy.name,
                "importance": self.metadata.importance,
                "confidence": self.metadata.confidence,
                "source_event": self.metadata.source_event.name if self.metadata.source_event else None,
                "tags": list(self.metadata.tags),
                "categories": list(self.metadata.categories),
                # relations are required for cascade forget to discover
                # in-edges after a backup is restored. The previous omission
                # caused restore-from-file to silently drop the knowledge
                # graph, breaking GDPR Article 17 delete-by-id traversal.
                "relations": [
                    {
                        "type": r.relation_type,
                        "target": r.target_memory_id,
                        "bidirectional": r.bidirectional,
                        "strength": r.strength,
                    }
                    for r in self.metadata.relations
                ],
                "provenance": self.metadata.provenance,
            },
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Memory:
        """Deserialize from JSON-compatible dict.

        Reads ``embedding`` (payload) and ``relations`` (metadata) so the
        roundtrip is lossless. Missing keys default to ``None`` / empty
        list to stay backward compatible with backups written before
        v0.4.x.
        """
        meta = data.get("metadata", {})
        return cls(
            id=MemoryId(data["id"]),
            anchor=TemporalAnchor(
                created_at=data["anchor"]["created_at"],
                accessed_at=data["anchor"].get("accessed_at"),
                consolidated_at=data["anchor"].get("consolidated_at"),
                expires_at=data["anchor"].get("expires_at"),
            ),
            context=AgentContext(
                agent_id=data["context"]["agent_id"],
                agent_type=data["context"]["agent_type"],
                session_id=data["context"]["session_id"],
                user_id=data["context"].get("user_id"),
                team_id=data["context"].get("team_id"),
                project_id=data["context"].get("project_id"),
                tenant_id=data["context"].get("tenant_id"),
            ),
            payload=MemoryPayload(
                raw=data["payload"]["raw"],
                structured=data["payload"].get("structured"),
                # embedding defaults to None if missing (older backups).
                embedding=data["payload"].get("embedding"),
            ),
            metadata=MemoryMetadata(
                memory_type=MemoryType[meta["memory_type"]],
                privacy=PrivacyLevel[meta["privacy"]],
                importance=meta["importance"],
                confidence=meta["confidence"],
                source_event=EventType[meta["source_event"]] if meta.get("source_event") else None,
                tags=set(meta.get("tags", [])),
                categories=set(meta.get("categories", [])),
                # relations default to [] if missing (older backups).
                relations=[
                    Relation(
                        r["type"],
                        r["target"],
                        bidirectional=r.get("bidirectional", False),
                        strength=r.get("strength", 1.0),
                    )
                    for r in meta.get("relations", [])
                ],
                provenance=meta.get("provenance", []),
            ),
        )


@dataclass
class AgentEvent:
    """Any observable event in an agent's lifecycle."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType = EventType.ENV_OBSERVATION
    timestamp: float = field(default_factory=time.time)

    # Who
    agent_context: AgentContext = field(default_factory=lambda: AgentContext("", "", ""))

    # What
    content: str = ""                          # Natural language description
    structured_data: dict[str, Any] | None = None  # JSON-serializable artifacts
    attachments: list[dict[str, Any]] = field(default_factory=list)  # Images, audio, files

    # Why / Context
    intent: str | None = None             # Agent's goal at this moment
    plan_id: str | None = None            # Which plan this belongs to
    parent_event_id: str | None = None   # Causal chain

    # Privacy
    privacy: PrivacyLevel = PrivacyLevel.PUBLIC

    def to_memory(self) -> Memory:
        """Convert this event into a Working-tier memory."""
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(
                created_at=self.timestamp,
                expires_at=self.timestamp + 1800,  # 30 min TTL default
            ),
            context=self.agent_context,
            payload=MemoryPayload(
                raw=self.content,
                structured=self.structured_data,
            ),
            metadata=MemoryMetadata(
                memory_type=MemoryType.WORKING,
                privacy=self.privacy,
                source_event=self.event_type,
                tags={self.event_type.name, self.agent_context.agent_type},
            ),
        )
