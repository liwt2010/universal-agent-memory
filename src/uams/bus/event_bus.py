"""Event bus for universal agent event distribution.

Thread-safe implementation with RLock.
"""

from __future__ import annotations

import threading
from typing import Protocol
from collections import defaultdict

from uams.core.enums import EventType
from uams.core.models import AgentEvent
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class EventHandler(Protocol):
    """Protocol for any component that observes agent lifecycle."""

    def handle(self, event: AgentEvent) -> None:
        ...


class EventBus:
    """
    Central event distribution hub.
    Zero framework coupling - any agent can publish events.
    Thread-safe with RLock.
    """

    def __init__(self, max_buffer_size: int = 1000):
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []
        self._history: list[AgentEvent] = []
        self._max_buffer_size = max_buffer_size
        self._lock = threading.RLock()

    def subscribe(
        self,
        handler: EventHandler,
        event_types: list[EventType] | None = None,
    ) -> None:
        with self._lock:
            if event_types is None:
                self._global_handlers.append(handler)
            else:
                for et in event_types:
                    self._handlers[et].append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        with self._lock:
            if handler in self._global_handlers:
                self._global_handlers.remove(handler)
            for handlers in self._handlers.values():
                if handler in handlers:
                    handlers.remove(handler)

    def publish(self, event: AgentEvent) -> None:
        """
        Publish an event to all subscribers and append to history buffer.
        Thread-safe. Handler exceptions are isolated (one failure doesn't stop others).
        """
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_buffer_size:
                self._history.pop(0)
            # Snapshot handlers while holding lock to avoid mutation during iteration
            global_handlers = list(self._global_handlers)
            type_handlers = list(self._handlers.get(event.event_type, []))

        # Execute handlers outside the lock to prevent deadlocks
        for handler in global_handlers + type_handlers:
            try:
                handler.handle(event)
            except Exception:
                logger.exception(
                    "Handler %s failed for event %s (type=%s). Isolating failure.",
                    handler,
                    event.event_id,
                    event.event_type.name,
                )

    def get_recent(self, n: int = 50) -> list[AgentEvent]:
        with self._lock:
            return self._history[-n:]

    def get_events_by_type(
        self,
        event_type: EventType,
        limit: int = 50,
    ) -> list[AgentEvent]:
        with self._lock:
            matching = [e for e in self._history if e.event_type == event_type]
            return matching[-limit:]

    def get_events_by_session(
        self,
        session_id: str,
    ) -> list[AgentEvent]:
        with self._lock:
            return [e for e in self._history if e.agent_context.session_id == session_id]

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
