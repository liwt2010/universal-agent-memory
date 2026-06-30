"""Example: Customer Service (Multi-ticket Case)"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import UniversalMemorySystem, AgentContext, AgentEvent, EventType


def demo():
    print("\n" + "=" * 60)
    print("EXAMPLE: Customer Service (Multi-ticket Case)")
    print("=" * 60)

    ums = UniversalMemorySystem()

    # --- Ticket 1: Initial complaint ---
    ctx = AgentContext(
        agent_id="support_001",
        agent_type="customer_service",
        session_id="ticket_2847",
        user_id="customer_corp_acme",
        team_id="enterprise_support",
    )

    ums.observe(AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=ctx,
        content="Our enterprise dashboard is loading slowly. We have 500 users affected.",
        structured_data={
            "fact": "Acme Corp dashboard slow, 500 users affected",
            "importance": 8.0,
            "category": "incident",
        },
    ))

    ums.observe(AgentEvent(
        event_type=EventType.ACTION_END,
        agent_context=ctx,
        content="Diagnosed: N+1 query in user_metrics endpoint. Fixed by adding select_related.",
        structured_data={
            "fact": "N+1 query in user_metrics endpoint, fixed with select_related",
            "importance": 7.0,
            "category": "technical_resolution",
        },
    ))

    ums.observe(AgentEvent(
        event_type=EventType.SESSION_END,
        agent_context=ctx,
        content="Ticket 2847 resolved",
    ))

    print("\n[Ticket 1 complete. Memories consolidated.]")
    print(f"Stats: {ums.get_stats()}")

    # --- Ticket 2 (3 days later): Follow-up, same customer ---
    ctx2 = AgentContext(
        agent_id="support_002",  # Different agent on shift
        agent_type="customer_service",
        session_id="ticket_2912",
        user_id="customer_corp_acme",
        team_id="enterprise_support",
    )

    # New agent recalls previous ticket
    prev = ums.recall(
        "Acme Corp dashboard performance",
        context=ctx2,
        budget_tokens=800,
    )

    print(f"\nSupport Agent 2 recalls ({len(prev)} memories):")
    for mem in prev:
        print(f"  [score={mem.retrieval_score:.2f} | {mem.metadata.memory_type.name}] {mem.payload.raw[:100]}...")

    # Agent can say: "I see we previously fixed an N+1 query in your dashboard. Is this related?"
    print("\n[Agent 2 responds with historical context]")

    return ums


if __name__ == "__main__":
    demo()
