"""Agent framework adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from uams.core.models import AgentEvent, AgentContext


class AgentAdapter(ABC):
    """
    Base adapter for integrating UAMS with any agent framework.

    Subclasses implement hooks into the agent lifecycle and translate
    framework events into UAMS AgentEvents.
    """

    def __init__(self, uams_system: Any, agent_context: AgentContext):
        self.uams = uams_system
        self.context = agent_context

    @abstractmethod
    def on_user_input(self, content: str, **kwargs: Any) -> None:
        """Handle user input."""
        ...

    @abstractmethod
    def on_agent_output(self, content: str, **kwargs: Any) -> None:
        """Handle agent output."""
        ...

    @abstractmethod
    def on_action_start(self, action_name: str, **kwargs: Any) -> None:
        """Handle action start."""
        ...

    @abstractmethod
    def on_action_end(self, action_name: str, result: Any, **kwargs: Any) -> None:
        """Handle action completion."""
        ...

    @abstractmethod
    def on_session_start(self) -> None:
        """Handle session start."""
        ...

    @abstractmethod
    def on_session_end(self) -> None:
        """Handle session end."""
        ...


class SimpleAdapter(AgentAdapter):
    """
    Minimal adapter that directly calls uams.observe() for each event.
    Suitable for any framework where you can add manual hooks.
    """

    from uams.core.enums import EventType

    def on_user_input(self, content: str, **kwargs: Any) -> None:
        event = AgentEvent(
            event_type=self.EventType.USER_INPUT,
            agent_context=self.context,
            content=content,
            structured_data=kwargs.get("structured_data"),
            privacy=kwargs.get("privacy", self.EventType.PrivacyLevel.PUBLIC),
        )
        self.uams.observe(event)

    def on_agent_output(self, content: str, **kwargs: Any) -> None:
        event = AgentEvent(
            event_type=self.EventType.AGENT_OUTPUT,
            agent_context=self.context,
            content=content,
            structured_data=kwargs.get("structured_data"),
        )
        self.uams.observe(event)

    def on_action_start(self, action_name: str, **kwargs: Any) -> None:
        event = AgentEvent(
            event_type=self.EventType.ACTION_START,
            agent_context=self.context,
            content=f"Starting action: {action_name}",
            structured_data={"action": action_name},
        )
        self.uams.observe(event)

    def on_action_end(self, action_name: str, result: Any, **kwargs: Any) -> None:
        event = AgentEvent(
            event_type=self.EventType.ACTION_END,
            agent_context=self.context,
            content=f"Completed action: {action_name}",
            structured_data={"action": action_name, "result": result},
        )
        self.uams.observe(event)

    def on_session_start(self) -> None:
        event = AgentEvent(
            event_type=self.EventType.SESSION_START,
            agent_context=self.context,
            content="Session started",
        )
        self.uams.observe(event)

    def on_session_end(self) -> None:
        event = AgentEvent(
            event_type=self.EventType.SESSION_END,
            agent_context=self.context,
            content="Session ended",
        )
        self.uams.observe(event)
