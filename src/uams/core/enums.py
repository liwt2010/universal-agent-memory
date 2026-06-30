"""Core enumerations for the Universal Agent Memory System."""

from enum import Enum, auto


class MemoryType(Enum):
    """Universal memory categorization. No domain-specific types."""
    WORKING = auto()      # Raw, unprocessed sensory/events (seconds-minutes TTL)
    EPISODIC = auto()     # Structured experiences, session summaries (hours-days TTL)
    SEMANTIC = auto()     # Facts, preferences, concepts, beliefs (long-term)
    PROCEDURAL = auto()   # Skills, workflows, patterns, strategies (long-term)


class EventType(Enum):
    """Universal agent lifecycle events. Framework-agnostic."""
    # Perception layer
    ENV_OBSERVATION = auto()      # Agent observed environment state
    USER_INPUT = auto()           # User message/query
    AGENT_OUTPUT = auto()         # Agent response/decision

    # Action layer
    ACTION_START = auto()         # Tool/action execution begins
    ACTION_END = auto()           # Tool/action execution completes
    ACTION_FAILURE = auto()       # Tool/action failed

    # Meta-cognition layer
    PLAN_FORMED = auto()          # Agent formed a plan/intention
    PLAN_EXECUTED = auto()        # Plan step completed
    PLAN_ABORTED = auto()         # Plan abandoned
    REFLECTION = auto()           # Agent self-reflection

    # Session lifecycle
    SESSION_START = auto()
    SESSION_END = auto()
    SUBSESSION_START = auto()     # Sub-task / sub-agent spawned
    SUBSESSION_END = auto()

    # Social / Multi-agent
    SIGNAL_RECEIVED = auto()      # Message from another agent
    SIGNAL_SENT = auto()          # Message sent to another agent
    LEASE_ACQUIRED = auto()       # Exclusive resource lock
    LEASE_RELEASED = auto()


class PrivacyLevel(Enum):
    """Privacy classification for memories."""
    PUBLIC = auto()       # Safe to share across agents
    INTERNAL = auto()     # Within same agent instance
    PRIVATE = auto()      # User-specific, sensitive
    SECRET = auto()       # Credentials, PII (never leave local storage)
