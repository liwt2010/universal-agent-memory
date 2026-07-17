"""End-to-end auto-extract API.

v0.7.0 (audit item 1): ``uams.auto_extract`` — a single call
that takes a conversation and returns the episodic + semantic
memories that the LLM extracted from it.

Workflow:
  1. Validate the conversation shape.
  2. Build a transient AgentContext for the conversation
     (caller-supplied; AutoExtract does not invent one).
  3. Wrap each message in an AgentEvent with the right
     privacy level.
  4. observe() each event into the working store.
  5. consolidate() the session — this triggers
     LLMCompressionEngine.compress_working_to_episodic
     (episodic summary) and extract_semantic (atomic facts).
  6. Return the resulting episodic memory + the list of
     semantic-fact memories to the caller. The caller is
     responsible for showing them to the user; UAMS just
     runs the LLM and stores.

This is the engine-level capability that Vault
(product layer) calls. Vault's job is auth + billing +
display; UAMS's job is "given a conversation, where
should these memories live".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from uams.core.enums import EventType, PrivacyLevel
from uams.core.models import (
    AgentContext,
    AgentEvent,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    TemporalAnchor,
)
from uams.system import UniversalMemorySystem
from uams.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AutoExtractResult:
    """Result of a single ``auto_extract`` call.

    Fields:
        episodic: the consolidated episodic memory for the
            conversation (None if consolidation produced nothing
            — e.g. the LLM was disabled or the heuristic
            fallback was empty).
        semantic_facts: the atomic facts the LLM extracted
            from the episodic summary. May be empty if the
            LLM found no extractable facts.
        skipped: a list of (index, reason) tuples for any
            conversation messages that were not ingested
            (e.g. empty content, missing role).
        raw_event_count: how many AgentEvents were observed
            (== number of non-skipped messages).
        raw_consolidate_result: the underlying
            ConsolidateResult from consolidate() — exposed
            for callers that want duration_ms or the source
            event count.
    """

    episodic: Memory | None
    semantic_facts: list[Memory] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    raw_event_count: int = 0
    raw_consolidate_result: object = None  # ConsolidateResult


def _normalise_messages(
    conversation: str | list[dict[str, str]],
) -> list[dict[str, str]]:
    """Coerce the conversation into a list of {role, content}.

    Accepts:
      - a plain string (treated as a single user message)
      - a list of {"role": ..., "content": ...} dicts
        (OpenAI / chat-style format)
    Other shapes raise TypeError.
    """
    if isinstance(conversation, str):
        return [{"role": "user", "content": conversation}]
    if isinstance(conversation, list):
        out: list[dict[str, str]] = []
        for m in conversation:
            if not isinstance(m, dict):
                raise TypeError(
                    f"auto_extract: each message must be a dict, "
                    f"got {type(m).__name__}"
                )
            role = m.get("role")
            content = m.get("content")
            if role not in ("user", "assistant", "system"):
                raise ValueError(
                    f"auto_extract: message role must be one of "
                    f"'user' | 'assistant' | 'system', got {role!r}"
                )
            if not content or not isinstance(content, str):
                raise ValueError(
                    f"auto_extract: message content must be a "
                    f"non-empty string, got {content!r}"
                )
            out.append({"role": role, "content": content})
        if not out:
            raise ValueError("auto_extract: conversation is empty")
        return out
    raise TypeError(
        f"auto_extract: conversation must be str or list, "
        f"got {type(conversation).__name__}"
    )


def auto_extract(
    system: UniversalMemorySystem,
    conversation: str | list[dict[str, str]],
    *,
    agent_id: str,
    agent_type: str,
    session_id: str,
    user_id: str | None = None,
    team_id: str | None = None,
    project_id: str | None = None,
    tenant_id: str | None = None,
    privacy: PrivacyLevel = PrivacyLevel.PRIVATE,
) -> AutoExtractResult:
    """Run end-to-end memory extraction on a conversation.

    v0.7.0: replaces the v0.6.x product-layer pattern of
    "manually call observe() for each message, then manually
    call consolidate()". The new single-call API lets the
    product layer (Vault) treat memory extraction as one
    op: send a transcript, get the structured memories back.

    The function is synchronous. For async UAMS callers
    (v0.6.0's AsyncUniversalMemorySystem), use
    ``await async_auto_extract(...)`` (added in a separate
    module when the async surface is hardened).
    """
    messages = _normalise_messages(conversation)

    ctx = AgentContext(
        agent_id=agent_id,
        agent_type=agent_type,
        session_id=session_id,
        user_id=user_id,
        team_id=team_id,
        project_id=project_id,
        tenant_id=tenant_id,
    )

    # 1. Build AgentEvents. Privacy level: caller chooses; we
    # default to PRIVATE because most LLM-extracted memories
    # are personal (PII risk). EventType mapping:
    #   user      -> USER_INPUT
    #   assistant -> AGENT_OUTPUT
    #   system    -> SESSION_START (system context for the session)
    base_ts = _now()
    events: list[AgentEvent] = []
    skipped: list[tuple[int, str]] = []
    for i, m in enumerate(messages):
        role = m["role"]
        if role == "user":
            et = EventType.USER_INPUT
        elif role == "assistant":
            et = EventType.AGENT_OUTPUT
        else:
            et = EventType.SESSION_START
        events.append(AgentEvent(
            event_type=et,
            agent_context=ctx,
            content=m["content"],
            privacy=privacy,
            timestamp=base_ts + i * 0.001,  # preserve order
        ))

    # 2. observe() each event into the working store.
    for ev in events:
        try:
            system.observe(ev)
        except Exception:
            logger.exception(
                "auto_extract: observe() raised for event %s; skipping",
                ev.event_id,
            )
            skipped.append((len(skipped), "observe_failed"))

    # 3. consolidate() the session.
    consolidate_result = system.consolidate(session_id)

    # 4. Pull the episodic memory + the semantic facts. The
    # consolidate() result already holds the episodic memory
    # id; we look it up to return the full Memory object.
    episodic_memory: Memory | None = None
    if consolidate_result.episodic_memory_id is not None:
        # UniversalMemorySystem has no top-level retrieve() — the
        # episodic memory lives in the EPISODIC store.
        try:
            epi_store = system._stores[MemoryType.EPISODIC]
            episodic_memory = epi_store.retrieve(
                str(consolidate_result.episodic_memory_id)
            )
        except Exception:
            logger.exception(
                "auto_extract: EPISODIC retrieve failed for id=%s",
                consolidate_result.episodic_memory_id,
            )
    # ConsolidateResult.semantic_facts is an int count, not a list
    # of IDs. The actual atomic-fact memories live in the SEMANTIC
    # store under the same tenant — we look them up by listing
    # the SEMANTIC tier and filtering on tenant_id.
    semantic_facts: list[Memory] = []
    if consolidate_result.semantic_facts > 0 and tenant_id:
        try:
            sem_store = system._stores[MemoryType.SEMANTIC]
            for mem in sem_store.list_all(limit=10_000):
                if getattr(mem.context, "tenant_id", None) == tenant_id:
                    semantic_facts.append(mem)
        except Exception:
            logger.exception("auto_extract: SEMANTIC tier walk failed")

    return AutoExtractResult(
        episodic=episodic_memory,
        semantic_facts=semantic_facts,
        skipped=skipped,
        raw_event_count=len(events),
        raw_consolidate_result=consolidate_result,
    )


def _now() -> float:
    """Single point of import for time.time so tests can mock it."""
    import time
    return time.time()
