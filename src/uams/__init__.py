"""Universal Agent Memory System (UAMS)

A domain-agnostic persistent memory layer for any AI agent.
Decoupled from coding, applicable to personal assistants, NPCs,
customer service, research agents, multi-agent systems, etc.
"""

__version__ = "0.5.2"

from uams.system import UniversalMemorySystem, ConsolidateResult
from uams.core.enums import MemoryType, EventType, PrivacyLevel
from uams.core.models import (
    MemoryId,
    TemporalAnchor,
    AgentContext,
    MemoryPayload,
    Relation,
    MemoryMetadata,
    Memory,
    AgentEvent,
)
from uams.bus.event_bus import EventBus
from uams.storage.base import MemoryStore
from uams.storage.memory import InMemoryStore
from uams.pipeline.retrieval import RetrievalPipeline
from uams.pipeline.compression import CompressionEngine, HeuristicCompressionEngine
from uams.pipeline.privacy import PrivacyFilter, DeduplicationWindow
from uams.pipeline.forgetting import ForgettingEngine
from uams.multi_agent.coordinator import MultiAgentCoordinator, Lease, Signal
from uams.errors import UAMSError, ConfigError, StorageError, CascadeError, LLMError

__all__ = [
    "UniversalMemorySystem",
    "ConsolidateResult",
    "MemoryType",
    "EventType",
    "PrivacyLevel",
    "MemoryId",
    "TemporalAnchor",
    "AgentContext",
    "MemoryPayload",
    "Relation",
    "MemoryMetadata",
    "Memory",
    "AgentEvent",
    "EventBus",
    "MemoryStore",
    "InMemoryStore",
    "RetrievalPipeline",
    "CompressionEngine",
    "HeuristicCompressionEngine",
    "PrivacyFilter",
    "DeduplicationWindow",
    "ForgettingEngine",
    "MultiAgentCoordinator",
    "Lease",
    "Signal",
    "UAMSError",
    "ConfigError",
    "StorageError",
    "CascadeError",
    "LLMError",
]
