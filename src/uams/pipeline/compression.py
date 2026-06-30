"""Memory compression and consolidation engines."""

from abc import ABC, abstractmethod
from collections import defaultdict
from typing import List

from uams.core.enums import MemoryType, PrivacyLevel, EventType
from uams.core.models import Memory, MemoryId, TemporalAnchor, AgentEvent, MemoryPayload, MemoryMetadata


class CompressionEngine(ABC):
    """
    Abstract compression engine.

    Implementations may use LLM-based summarization, rule-based heuristics,
    or hybrid approaches.
    """

    @abstractmethod
    def compress_working_to_episodic(self, events: List[AgentEvent]) -> Memory:
        """Convert raw event stream into a structured episodic memory."""
        ...

    @abstractmethod
    def extract_semantic(self, episodic: Memory) -> List[Memory]:
        """Extract atomic facts, preferences, concepts from an episodic memory."""
        ...

    @abstractmethod
    def extract_procedural(self, episodes: List[Memory]) -> List[Memory]:
        """Extract reusable workflows, strategies, patterns across episodes."""
        ...


class HeuristicCompressionEngine(CompressionEngine):
    """
    Non-LLM fallback: rule-based consolidation.

    Suitable for environments without LLM access or for lightweight deployments.
    """

    def compress_working_to_episodic(self, events: List[AgentEvent]) -> Memory:
        if not events:
            raise ValueError("No events to compress")

        narrative_parts: List[str] = []
        tags: set = set()
        categories: set = set()

        for e in events:
            narrative_parts.append(f"[{e.event_type.name}] {e.content}")
            if e.intent:
                tags.add(f"intent:{e.intent}")
            if e.structured_data:
                for k in e.structured_data.keys():
                    categories.add(k)

        first, last = events[0], events[-1]

        import time
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(
                created_at=first.timestamp,
                consolidated_at=time.time(),
            ),
            context=first.agent_context,
            payload=MemoryPayload(
                raw="\n".join(narrative_parts),
                structured={
                    "event_count": len(events),
                    "duration_sec": last.timestamp - first.timestamp,
                    "intents": list(tags),
                },
            ),
            metadata=MemoryMetadata(
                memory_type=MemoryType.EPISODIC,
                privacy=first.privacy,
                tags=tags,
                categories=categories,
                source_event=EventType.SESSION_END,
            ),
        )

    def extract_semantic(self, episodic: Memory) -> List[Memory]:
        """Naive: extract structured fields as atomic facts."""
        facts: List[Memory] = []
        if episodic.payload.structured:
            import time
            for key, value in episodic.payload.structured.items():
                if isinstance(value, (str, int, float, bool)):
                    facts.append(Memory(
                        id=MemoryId(),
                        anchor=TemporalAnchor(created_at=time.time()),
                        context=episodic.context,
                        payload=MemoryPayload(
                            raw=f"{key} = {value}",
                            structured={"key": key, "value": value},
                        ),
                        metadata=MemoryMetadata(
                            memory_type=MemoryType.SEMANTIC,
                            privacy=episodic.metadata.privacy,
                            categories={"extracted_fact"},
                            provenance=[str(episodic.id)],
                        ),
                    ))
        return facts

    def extract_procedural(self, episodes: List[Memory]) -> List[Memory]:
        """Identify repeated categories across episodes as procedural patterns."""
        category_counts = defaultdict(int)
        for ep in episodes:
            for cat in ep.metadata.categories:
                category_counts[cat] += 1

        procedures: List[Memory] = []
        import time
        for cat, count in category_counts.items():
            if count >= 2:
                procedures.append(Memory(
                    id=MemoryId(),
                    anchor=TemporalAnchor(created_at=time.time()),
                    context=episodes[0].context,
                    payload=MemoryPayload(
                        raw=f"Recurring pattern: {cat} (observed {count} times)",
                        structured={"pattern": cat, "frequency": count},
                    ),
                    metadata=MemoryMetadata(
                        memory_type=MemoryType.PROCEDURAL,
                        privacy=PrivacyLevel.PUBLIC,
                        categories={"pattern", "procedure"},
                    ),
                ))
        return procedures
