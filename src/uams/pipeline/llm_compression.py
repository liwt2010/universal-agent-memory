"""LLM-based memory compression engine.

Provides ``LLMCompressionEngine`` which uses an injected ``LLMClient``
to summarize raw events into episodic narratives, extract atomic semantic
facts, and identify recurring procedural patterns.

Falls back gracefully if the LLM client raises: logs a warning and
returns an empty / minimal result so the calling pipeline continues.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from uams.core.enums import EventType, MemoryType, PrivacyLevel
from uams.core.models import (
    AgentEvent,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    TemporalAnchor,
)
from uams.llm.client import LLMClient
from uams.pipeline.compression import CompressionEngine

logger = logging.getLogger(__name__)


# --- Prompt templates ---
# Kept short and fixed so providers with prompt caching (OpenAI auto-cache on
# stable prefix; Anthropic via cache_control; MiniMax equivalents) can reuse
# the cached prefix across calls. Each prompt reduced ~50% from the original.

_EPISODIC_SYSTEM = (
    "Summarize events into a <=200-word narrative. "
    "Preserve names, dates, numbers, preferences. Output only the narrative."
)

_EPISODIC_USER_TEMPLATE = (
    "Context: agent={agent_id} user={user_id} session={session_id}\n"
    "Events:\n{events}\n"
    "Narrative:"
)

_SEMANTIC_SYSTEM = (
    "Extract atomic user facts as JSON array: "
    "[{\"key\": str, \"value\": str}]. Skip session-specific info. Output only JSON."
)

_PROCEDURAL_SYSTEM = (
    "Find recurring workflows across sessions. Return JSON array: "
    "[{\"pattern\": str, \"description\": str, \"frequency\": int}]. "
    "Only patterns seen >=2 times. Output only JSON."
)


class LLMCompressionEngine(CompressionEngine):
    """LLM-backed compression engine. Inherits ``CompressionEngine`` contract."""

    def __init__(
        self,
        llm_client: LLMClient,
        max_events_per_call: int = 20,
        target_ratio: float = 0.3,
        timeout: float = 30.0,
    ):
        self._llm = llm_client
        self._max_events = max(1, int(max_events_per_call))
        self._target_ratio = float(target_ratio)
        self._timeout = float(timeout)

    # --- Episodic: events -> narrative Memory ---

    def compress_working_to_episodic(self, events: List[AgentEvent]) -> Memory:
        if not events:
            raise ValueError("No events to compress")

        first = events[0]
        ctx = first.agent_context

        # Batch if too many events: summarize each chunk, then summarize the summaries.
        if len(events) <= self._max_events:
            narrative = self._summarize_batch(events)
        else:
            chunk_summaries: List[str] = []
            for i in range(0, len(events), self._max_events):
                chunk = events[i : i + self._max_events]
                chunk_summaries.append(self._summarize_batch(chunk))
            pseudo_events: List[AgentEvent] = []
            base_ts = events[0].timestamp
            step = max(1e-3, (events[-1].timestamp - base_ts) / max(1, len(chunk_summaries)))
            for i, s in enumerate(chunk_summaries):
                pseudo_events.append(
                    AgentEvent(
                        event_type=EventType.SESSION_END,
                        agent_context=ctx,
                        content=s,
                        timestamp=base_ts + step * i,
                    )
                )
            narrative = self._summarize_batch(pseudo_events)

        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(
                created_at=events[0].timestamp,
                consolidated_at=time.time(),
            ),
            context=ctx,
            payload=MemoryPayload(
                raw=narrative,
                structured={
                    "event_count": len(events),
                    "duration_sec": events[-1].timestamp - events[0].timestamp,
                    "compression_engine": "llm",
                },
            ),
            metadata=MemoryMetadata(
                memory_type=MemoryType.EPISODIC,
                privacy=first.privacy,
                source_event=EventType.SESSION_END,
            ),
        )

    def _summarize_batch(self, events: List[AgentEvent]) -> str:
        """Call LLM to summarize a batch of events. Fallback to heuristic on error."""
        try:
            # Drop timestamp — it adds ~10 chars per event with no value to the LLM
            # for narrative summarization (the order of events conveys recency).
            events_text = "\n".join(
                f"[{e.event_type.name}] {e.content}" for e in events
            )
            ctx = events[0].agent_context
            user_msg = _EPISODIC_USER_TEMPLATE.format(
                agent_id=ctx.agent_id,
                user_id=ctx.user_id or "_",
                session_id=ctx.session_id,
                events=events_text,
            )
            return self._llm.chat(
                [
                    {"role": "system", "content": _EPISODIC_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=512,
                temperature=0.0,
                timeout=self._timeout,
            ).strip()
        except Exception:
            logger.exception(
                "LLM episodic summarization failed; using raw concatenation fallback"
            )
            return "\n".join(f"[{e.event_type.name}] {e.content}" for e in events)

    # --- Semantic: episodic narrative -> atomic facts ---

    def extract_semantic(self, episodic: Memory) -> List[Memory]:
        try:
            raw = self._llm.chat(
                [
                    {"role": "system", "content": _SEMANTIC_SYSTEM},
                    {"role": "user", "content": episodic.payload.raw},
                ],
                max_tokens=512,
                temperature=0.0,
                timeout=self._timeout,
            ).strip()
            facts_json = self._parse_json_array(raw)
        except Exception:
            logger.exception("LLM semantic extraction failed; returning empty list")
            return []

        facts: List[Memory] = []
        for item in facts_json:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            value = str(item.get("value", "")).strip()
            if not key or not value:
                continue
            facts.append(
                Memory(
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
                )
            )
        return facts

    # --- Procedural: episodes -> recurring patterns ---

    def extract_procedural(self, episodes: List[Memory]) -> List[Memory]:
        if len(episodes) < 2:
            return []
        try:
            joined = "\n\n---\n\n".join(ep.payload.raw for ep in episodes)
            raw = self._llm.chat(
                [
                    {"role": "system", "content": _PROCEDURAL_SYSTEM},
                    {"role": "user", "content": joined},
                ],
                max_tokens=512,
                temperature=0.0,
                timeout=self._timeout,
            ).strip()
            patterns = self._parse_json_array(raw)
        except Exception:
            logger.exception("LLM procedural extraction failed; returning empty list")
            return []

        procs: List[Memory] = []
        for item in patterns:
            if not isinstance(item, dict):
                continue
            name = str(item.get("pattern", "")).strip()
            desc = str(item.get("description", "")).strip()
            freq_raw = item.get("frequency", 0)
            try:
                freq = int(freq_raw)
            except (TypeError, ValueError):
                continue
            if not name or freq < 2:
                continue
            procs.append(
                Memory(
                    id=MemoryId(),
                    anchor=TemporalAnchor(created_at=time.time()),
                    context=episodes[0].context,
                    payload=MemoryPayload(
                        raw=f"{name}: {desc} (observed {freq} times)",
                        structured={"pattern": name, "frequency": freq},
                    ),
                    metadata=MemoryMetadata(
                        memory_type=MemoryType.PROCEDURAL,
                        privacy=PrivacyLevel.PUBLIC,
                        categories={"pattern", "procedure"},
                    ),
                )
            )
        return procs

    # --- Helpers ---

    @staticmethod
    def _parse_json_array(text: str) -> List[Any]:
        """Parse JSON array from LLM output. Tolerant of ```json fences."""
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown code fence (``` or ```json)
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                data = json.loads(text[start : end + 1])
            else:
                raise
        if not isinstance(data, list):
            raise ValueError("LLM output is not a JSON array")
        return data