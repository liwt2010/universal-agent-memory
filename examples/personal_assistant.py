"""Example: Personal Assistant (Travel Planning)"""

import sys
import os

# Add src to path so we can import uams without installation
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import UniversalMemorySystem, AgentContext, AgentEvent, EventType


def demo():
    print("=" * 60)
    print("EXAMPLE: Personal Assistant (Travel Planning)")
    print("=" * 60)

    ums = UniversalMemorySystem()

    # --- Day 1: User mentions preferences ---
    ctx = AgentContext(
        agent_id="pa_001",
        agent_type="personal_assistant",
        session_id="sess_day1",
        user_id="user_alice",
    )

    ums.observe(AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=ctx,
        content="I'm planning a trip to Japan. I prefer small boutique hotels, not big chains. I hate crowds.",
        structured_data={
            "fact": "Alice prefers boutique hotels over chains",
            "importance": 8.0,
            "category": "travel_preference",
        },
    ))

    ums.observe(AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=ctx,
        content="I'm vegetarian, please make sure restaurants accommodate that.",
        structured_data={
            "fact": "Alice is vegetarian",
            "importance": 9.0,
            "category": "dietary_restriction",
        },
    ))

    # Day 1 session ends -> consolidation triggers
    ums.observe(AgentEvent(
        event_type=EventType.SESSION_END,
        agent_context=ctx,
        content="Day 1 session ended",
    ))

    print("\n[Day 1 complete. Memories consolidated.]")
    print(f"Stats: {ums.get_stats()}")

    # --- Day 2: New session, agent should recall preferences ---
    ctx2 = AgentContext(
        agent_id="pa_001",
        agent_type="personal_assistant",
        session_id="sess_day2",
        user_id="user_alice",
    )

    # Before responding, recall relevant context
    relevant = ums.recall(
        "Japan trip hotel recommendations",
        context=ctx2,
        budget_tokens=1000,
    )

    print(f"\nDay 2 Recall Results ({len(relevant)} memories):")
    for mem in relevant:
        print(f"  [score={mem.retrieval_score:.2f} | {mem.metadata.memory_type.name}] {mem.payload.raw[:80]}...")

    # Inject context as a prompt block
    context_block = ums.inject_context(
        "Japan trip hotel recommendations",
        context=ctx2,
        budget_tokens=1000,
    )
    print("\n--- Injected Context Block ---")
    print(context_block)
    print("--- End Context Block ---")

    return ums


if __name__ == "__main__":
    demo()
