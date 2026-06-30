"""Example: Research Agent (Iterative Literature Review)"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import UniversalMemorySystem, AgentContext, AgentEvent, EventType


def demo():
    print("\n" + "=" * 60)
    print("EXAMPLE: Research Agent (Iterative Literature Review)")
    print("=" * 60)

    ums = UniversalMemorySystem()

    # --- Iteration 1: Agent explores topic ---
    ctx = AgentContext(
        agent_id="researcher_01",
        agent_type="research_agent",
        session_id="research_sess_1",
        project_id="neural_manifolds",
    )

    ums.observe(AgentEvent(
        event_type=EventType.ACTION_END,
        agent_context=ctx,
        content="Search results: 5 papers on manifold hypothesis in neural networks. Key paper: 'Neural Manifolds' by Stringer et al.",
        structured_data={
            "fact": "Stringer et al. paper on neural manifolds is key reference",
            "importance": 9.0,
            "category": "paper_reference",
        },
    ))

    ums.observe(AgentEvent(
        event_type=EventType.REFLECTION,
        agent_context=ctx,
        content="Hypothesis: Dimensionality reduction in neural representations follows manifold structure. Need topology verification.",
        structured_data={
            "fact": "Research hypothesis: neural representations follow manifold structure, verify with topology",
            "importance": 8.0,
            "category": "research_hypothesis",
        },
    ))

    ums.observe(AgentEvent(
        event_type=EventType.SESSION_END,
        agent_context=ctx,
        content="Research session 1 complete",
    ))

    print("\n[Iteration 1 complete. Memories consolidated.]")
    print(f"Stats: {ums.get_stats()}")

    # --- Iteration 2 (next week): Agent continues ---
    ctx2 = AgentContext(
        agent_id="researcher_01",
        agent_type="research_agent",
        session_id="research_sess_2",
        project_id="neural_manifolds",
    )

    # Recall previous findings and hypothesis
    findings = ums.recall(
        "manifold hypothesis topology methods",
        context=ctx2,
        budget_tokens=1500,
    )

    print(f"\nResearch Iteration 2 recalls ({len(findings)} memories):")
    for mem in findings:
        print(f"  [{mem.metadata.memory_type.name}] score={mem.retrieval_score:.2f}: {mem.payload.raw[:90]}...")

    print("\n[Agent resumes research with full context]")

    return ums


if __name__ == "__main__":
    demo()
