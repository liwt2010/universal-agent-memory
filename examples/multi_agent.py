"""Example: Multi-Agent Collaboration"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import (
    UniversalMemorySystem,
    AgentContext,
    AgentEvent,
    EventType,
    Memory,
    MemoryId,
    MemoryPayload,
    MemoryMetadata,
    TemporalAnchor,
    PrivacyLevel,
    MemoryType,
    Signal,
    InMemoryStore,
)


def demo():
    print("\n" + "=" * 60)
    print("EXAMPLE: Multi-Agent Collaboration")
    print("=" * 60)

    shared = InMemoryStore()
    ums = UniversalMemorySystem()
    ums.enable_multi_agent(shared)

    # --- Agent A (Data Collection) gathers info ---
    ctx_a = AgentContext(
        agent_id="agent_data",
        agent_type="data_collection",
        session_id="task_001",
        team_id="analysis_team",
    )

    ums.observe(AgentEvent(
        event_type=EventType.ACTION_END,
        agent_context=ctx_a,
        content="Dataset collected: 10,000 customer records with satisfaction scores.",
        structured_data={
            "fact": "Dataset: 10,000 customer records with satisfaction scores available",
            "importance": 7.0,
            "category": "data_asset",
        },
    ))

    # Agent A shares to team space
    team_mem = Memory(
        id=MemoryId(),
        anchor=TemporalAnchor(),
        context=ctx_a,
        payload=MemoryPayload(raw="Dataset available for analysis team"),
        metadata=MemoryMetadata(
            memory_type=MemoryType.SEMANTIC,
            privacy=PrivacyLevel.PUBLIC,
            categories={"team_shared", "data"},
        ),
    )
    ums.share_memory(team_mem, target_team="analysis_team")

    # Agent A signals Agent B
    ums.send_signal(Signal(
        sender="agent_data",
        recipient="agent_analysis",
        signal_type="data_ready",
        payload={"dataset_size": 10000, "location": "/shared/data/customer_2024.csv"},
    ))

    # --- Agent B (Analysis) reads signals and shared memory ---
    ctx_b = AgentContext(
        agent_id="agent_analysis",
        agent_type="data_analysis",
        session_id="task_001",
        team_id="analysis_team",
    )

    signals = ums.read_signals("agent_analysis")
    print(f"\nAgent B received {len(signals)} signals:")
    for sig in signals:
        print(f"  From {sig.sender}: {sig.type} - {sig.payload}")

    # Agent B tries to acquire analysis task lease
    acquired = ums.acquire_lock("agent_analysis", "task_001_analysis")
    if acquired:
        print(f"  Agent B acquired lock on task_001_analysis")

    # Try again (should fail, already locked)
    acquired2 = ums.acquire_lock("agent_analysis_2", "task_001_analysis")
    print(f"  Second agent tried lock: {'acquired' if acquired2 else 'blocked'}")

    return ums


if __name__ == "__main__":
    demo()
