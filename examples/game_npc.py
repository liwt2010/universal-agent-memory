"""Example: Game NPC (Tavern Keeper)"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import UniversalMemorySystem, AgentContext, AgentEvent, EventType


def demo():
    print("\n" + "=" * 60)
    print("EXAMPLE: Game NPC (Tavern Keeper)")
    print("=" * 60)

    ums = UniversalMemorySystem()

    # --- First encounter ---
    ctx = AgentContext(
        agent_id="npc_tavern_keeper",
        agent_type="game_npc",
        session_id="encounter_1",
        user_id="player_bob",
    )

    ums.observe(AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=ctx,
        content="Player Bob insulted the tavern keeper and started a bar fight.",
        structured_data={
            "fact": "Bob insulted tavern keeper, started fight",
            "importance": 7.0,
            "category": "player_reputation",
        },
    ))

    ums.observe(AgentEvent(
        event_type=EventType.ACTION_END,
        agent_context=ctx,
        content="Tavern keeper called town guards. Bob was arrested.",
        structured_data={
            "fact": "Bob was arrested by town guards",
            "importance": 6.0,
            "category": "world_event",
        },
    ))

    ums.observe(AgentEvent(
        event_type=EventType.SESSION_END,
        agent_context=ctx,
        content="Encounter 1 ended",
    ))

    print("\n[First encounter complete. Memories consolidated.]")
    print(f"Stats: {ums.get_stats()}")

    # --- Second encounter (week later in game) ---
    ctx2 = AgentContext(
        agent_id="npc_tavern_keeper",
        agent_type="game_npc",
        session_id="encounter_2",
        user_id="player_bob",
    )

    # NPC recalls who this player is
    memories = ums.recall(
        "Bob tavern reputation",
        context=ctx2,
        budget_tokens=500,
    )

    print(f"\nNPC recalls ({len(memories)} memories):")
    for mem in memories:
        print(f"  [score={mem.retrieval_score:.2f} | {mem.metadata.memory_type.name}] {mem.payload.raw}")

    # NPC should say: "You again? You're not welcome here after last time."
    print("\n[NPC dialogue generated with memory context]")

    return ums


if __name__ == "__main__":
    demo()
