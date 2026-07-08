"""Token-compression comparison demo: Heuristic vs LLMCompressionEngine.

Generates a realistic 20-event agent session, then compresses it with both
engines (LLM mocked with a deterministic summary string) and reports the
resulting token counts.

Run: python examples/_token_compression_demo.py
"""

import os
import sys
import time
from typing import List

sys.path.insert(0, os.path.join(os.path.join(os.path.dirname(__file__), ".."), "src"))

from uams import (
    AgentContext,
    AgentEvent,
    EventType,
    MemoryId,
    MemoryPayload,
    MemoryMetadata,
    PrivacyLevel,
    TemporalAnchor,
    Memory,
    MemoryType,
)
from uams.llm.client import LLMClient
from uams.pipeline.compression import HeuristicCompressionEngine
from uams.pipeline.llm_compression import LLMCompressionEngine
from uams.utils.tokens import TokenEstimator


# Deterministic fake LLM: returns the same canned summary for any input.
class CannedLLM(LLMClient):
    SUMMARY = (
        "Alice (alice@example.com) is vegetarian and prefers boutique hotels. "
        "In this session she asked about a Japan trip in May 2026 (5 days, ~$3000 budget). "
        "She likes quiet neighborhoods and onsen hotels. "
        "Decisions: focus on Kyoto + Tokyo, mid-range boutique ryokans, vegetarian-friendly. "
        "Open: flight booking, JR pass, restaurant reservations."
    )

    def chat(self, messages, **kwargs):
        return self.SUMMARY


def make_events() -> List[AgentEvent]:
    """Realistic 20-event session for an Alice-type user."""
    ctx = AgentContext(
        agent_id="pa_001",
        agent_type="personal_assistant",
        session_id="sess_japan_2026",
        user_id="alice",
    )
    raw_events = [
        ("USER_INPUT", "Hi, I'm planning a trip to Japan in May 2026."),
        ("AGENT_OUTPUT", "Great! How many days and what's your budget?"),
        ("USER_INPUT", "About 5 days. Budget around $3000 excluding flights."),
        ("AGENT_OUTPUT", "Got it. Any preferences on hotels?"),
        ("USER_INPUT", "I prefer boutique hotels, quiet neighborhoods."),
        ("USER_INPUT", "Also I'm vegetarian."),
        ("AGENT_OUTPUT", "Noted. Vegetarian-friendly options are widely available."),
        ("USER_INPUT", "I'm interested in Kyoto and Tokyo. Skip Osaka this time."),
        ("AGENT_OUTPUT", "Sure. Should I focus on cultural sites or modern attractions?"),
        ("USER_INPUT", "Cultural: temples, gardens, traditional ryokans."),
        ("USER_INPUT", "Maybe one onsen experience."),
        ("AGENT_OUTPUT", "I'll find boutique ryokans with vegetarian kaiseki options."),
        ("USER_INPUT", "Also need help with flight booking and JR pass."),
        ("AGENT_OUTPUT", "I'll add flight search and JR pass recommendation."),
        ("REFLECTION", "User prefers boutique, vegetarian, cultural focus. Japan May 2026."),
        ("PLAN_FORMED", "Plan: 3 nights Kyoto + 2 nights Tokyo. Boutique ryokans."),
        ("ACTION_START", "Search boutique ryokans in Kyoto (vegetarian-friendly)."),
        ("ACTION_END", "Found 5 candidates."),
        ("USER_INPUT", "Show me the top 3."),
        ("SESSION_END", "End session, consolidate preferences into memory."),
    ]
    base_ts = time.time() - 3600  # 1 hour ago
    events: List[AgentEvent] = []
    for i, (et_name, content) in enumerate(raw_events):
        events.append(
            AgentEvent(
                event_type=EventType[et_name],
                agent_context=ctx,
                content=content,
                timestamp=base_ts + i * 30.0,
            )
        )
    return events


def episodic_token_count(memory: Memory, est: TokenEstimator) -> int:
    return est.estimate(memory.payload.raw)


def main():
    est = TokenEstimator()  # tiktoken if available, else heuristic
    print(f"Token estimator: tiktoken={'enabled' if est._encoder else 'heuristic'}\n")

    events = make_events()
    print(f"Session events: {len(events)}")
    raw_concat = "\n".join(f"[{e.event_type.name}] {e.content}" for e in events)
    raw_tokens = est.estimate(raw_concat)
    print(f"Raw concatenation (no compression): {raw_tokens} tokens\n")

    # --- Heuristic ---
    heuristic = HeuristicCompressionEngine()
    heuristic_mem = heuristic.compress_working_to_episodic(events)
    heuristic_tokens = episodic_token_count(heuristic_mem, est)
    print(f"HeuristicCompressionEngine:")
    print(f"  Episodic raw: {heuristic_tokens} tokens")
    print(f"  Preview: {heuristic_mem.payload.raw[:120]}...\n")

    # --- LLM Compression (mocked) ---
    llm_engine = LLMCompressionEngine(
        CannedLLM(),
        max_events_per_call=20,
        timeout=10.0,
    )
    llm_mem = llm_engine.compress_working_to_episodic(events)
    llm_tokens = episodic_token_count(llm_mem, est)
    print(f"LLMCompressionEngine (canned summary):")
    print(f"  Episodic raw: {llm_tokens} tokens")
    print(f"  Preview: {llm_mem.payload.raw[:120]}...\n")

    # --- Comparison ---
    print("=" * 60)
    print(f"Comparison (episodic narrative only):")
    print(f"  Raw concat:     {raw_tokens:>5} tokens")
    print(f"  Heuristic:      {heuristic_tokens:>5} tokens  ({heuristic_tokens / raw_tokens * 100:.0f}% of raw)")
    print(f"  LLM (canned):   {llm_tokens:>5} tokens  ({llm_tokens / raw_tokens * 100:.0f}% of raw)")
    print(f"  LLM savings vs heuristic: {(heuristic_tokens - llm_tokens) / heuristic_tokens * 100:.0f}%")
    print(f"  LLM savings vs raw:       {(raw_tokens - llm_tokens) / raw_tokens * 100:.0f}%")
    print()
    print(f"Note: with a real LLM, the summary length is bounded (~200 words ≈ 250 tokens)")
    print(f"      so absolute LLM token count stays roughly constant regardless of session size.")
    print(f"      That is the *real* token win: O(1) instead of O(N) in session length.")


if __name__ == "__main__":
    main()