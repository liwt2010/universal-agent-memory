"""Multi-agent coordination: leases, signals, and shared memory spaces.

Thread-safe implementation with RLock.
"""

from __future__ import annotations

import time
import threading
import uuid
from collections import defaultdict

from uams.core.enums import PrivacyLevel
from uams.core.models import Memory
from uams.storage.base import MemoryStore
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class Lease:
    """Exclusive resource lock for multi-agent cooperation."""

    def __init__(
        self,
        resource: str,
        holder: str,
        ttl: float = 300.0,
        context: str | None = None,
    ):
        self.lease_id = str(uuid.uuid4())
        self.resource = resource
        self.holder = holder
        self.acquired_at = time.time()
        self.expires_at = time.time() + ttl
        self.context = context

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class Signal:
    """Inter-agent message."""

    def __init__(
        self,
        sender: str,
        recipient: str,
        signal_type: str,
        payload: dict | None = None,
    ):
        self.signal_id = str(uuid.uuid4())
        self.sender = sender
        self.recipient = recipient  # "*" for broadcast
        self.type = signal_type
        self.payload = payload or {}
        self.timestamp = time.time()
        self.read_by: set[str] = set()


class MultiAgentCoordinator:
    """
    Shared memory spaces with access control, leases, and signal passing.
    Thread-safe with RLock. Supports Redis distributed locks for multi-process deployments.
    """

    # Bound on the in-memory signal queue. Long-running multi-agent
    # processes that emit broadcast signals would otherwise grow
    # self._signals monotonically (read_signals only marks them as
    # read; it never removes). When the queue exceeds this cap, the
    # oldest signals are dropped on append.
    MAX_SIGNALS = 10000

    def __init__(self, shared_store: MemoryStore, redis_client=None):
        self._shared = shared_store
        self._leases: dict[str, Lease] = {}
        self._signals: list[Signal] = []
        self._agent_scopes: dict[str, set[str]] = defaultdict(set)
        self._lock = threading.RLock()
        self._redis_client = redis_client  # Optional: Redis for distributed locks
        # Set to True the first time a Redis call raises. Once disabled,
        # lease methods short-circuit to None/False because in-memory
        # locking is meaningless in a multi-process deployment (which is
        # exactly why Redis was chosen). Other workers are unaffected
        # because each has its own MultiAgentCoordinator instance.
        self._disabled = False

    @property
    def is_disabled(self) -> bool:
        """True if a Redis call has failed and the coordinator dropped
        the distributed-coordination role for this process."""
        return self._disabled

    def _disable(self, reason: Exception) -> None:
        """Mark this coordinator instance as disabled after a Redis error."""
        if not self._disabled:
            logger.error(
                "MultiAgentCoordinator auto-disabling: distributed lock "
                "unavailable (%s: %s). Lease acquire/release will return "
                "None/False for this process. Other workers are unaffected.",
                type(reason).__name__, reason,
            )
            self._disabled = True

    def acquire_lease(
        self,
        agent_id: str,
        resource: str,
        ttl: float = 300.0,
        context: str | None = None,
    ) -> Lease | None:
        """
        Attempt to acquire an exclusive lease on a resource.
        Thread-safe. If redis_client is available, uses Redis distributed lock.

        Returns Lease if acquired, None if already locked by another agent
        or if the coordinator has been auto-disabled.
        """
        # If a previous Redis call failed, we cannot safely coordinate
        # across processes. Refuse instead of silently degrading to
        # in-memory locks (which would mislead the caller into thinking
        # their multi-process lease was held).
        if self._disabled:
            logger.warning(
                "acquire_lease(%s, %s) skipped: coordinator is disabled",
                agent_id, resource,
            )
            return None

        # Try Redis distributed lock first (multi-process safe)
        if self._redis_client and self._redis_client._available:
            try:
                lock_key = f"uams:lease:{resource}"
                lock_value = f"{agent_id}:{time.time()}"
                acquired = self._redis_client._client.set(
                    lock_key, lock_value, nx=True, ex=int(ttl)
                )
                if acquired:
                    logger.info(
                        "Agent %s acquired distributed lease on %s (ttl=%.0fs)",
                        agent_id, resource, ttl
                    )
                    return Lease(resource, agent_id, ttl, context)
                logger.warning(
                    "Distributed lease acquisition failed for %s: resource %s already held",
                    agent_id, resource
                )
                return None
            except Exception as exc:
                self._disable(exc)
                return None

        # No Redis available: use in-memory lock (single-process only).
        # We do NOT auto-disable on this path because the user opted in
        # to in-memory mode by not providing a Redis client.
        with self._lock:
            # Clean expired leases
            self._leases = {
                k: v for k, v in self._leases.items() if not v.is_expired()
            }

            if resource in self._leases:
                logger.warning(
                    "Lease acquisition failed for %s: resource %s already held by %s",
                    agent_id, resource, self._leases[resource].holder
                )
                return None

            new_lease = Lease(resource, agent_id, ttl, context)
            self._leases[resource] = new_lease
            logger.info(
                "Agent %s acquired in-memory lease on %s (ttl=%.0fs)",
                agent_id, resource, ttl
            )
            return new_lease

    def release_lease(self, agent_id: str, resource: str) -> bool:
        """Release a lease if held by this agent.

        Returns False (without contacting Redis) if the coordinator is
        disabled — the caller can detect that the distributed lease
        was not actually released and decide whether to retry or alert.
        """
        if self._disabled:
            logger.warning(
                "release_lease(%s, %s) skipped: coordinator is disabled",
                agent_id, resource,
            )
            return False

        # Try Redis distributed lock first
        if self._redis_client and self._redis_client._available:
            try:
                lock_key = f"uams:lease:{resource}"
                lock_value = self._redis_client._client.get(lock_key)
                if lock_value and lock_value.decode("utf-8", errors="ignore").startswith(agent_id + ":"):
                    self._redis_client._client.delete(lock_key)
                    logger.info("Agent %s released distributed lease on %s", agent_id, resource)
                    return True
                return False
            except Exception as exc:
                self._disable(exc)
                return False
        
        # Fallback to in-memory lock
        with self._lock:
            lease = self._leases.get(resource)
            if lease and lease.holder == agent_id:
                del self._leases[resource]
                logger.info("Agent %s released in-memory lease on %s", agent_id, resource)
                return True
            return False

    def send_signal(self, signal: Signal) -> None:
        """Send a signal to another agent or broadcast.

        The signal queue is bounded to ``MAX_SIGNALS``: when a new
        signal would push the queue over the cap, the oldest signal
        is dropped. This prevents unbounded memory growth in
        long-running agents that emit broadcast signals faster than
        they are consumed.
        """
        with self._lock:
            self._signals.append(signal)
            if len(self._signals) > self.MAX_SIGNALS:
                dropped = len(self._signals) - self.MAX_SIGNALS
                # Drop the oldest `dropped` signals. We do not preserve
                # them elsewhere; broadcast signals that no one read in
                # the last MAX_SIGNALS emissions are not delivered.
                self._signals = self._signals[dropped:]
                logger.warning(
                    "MultiAgentCoordinator signal queue exceeded cap (%d); "
                    "dropped %d oldest unread signals",
                    self.MAX_SIGNALS, dropped,
                )
            logger.debug(
                "Signal sent from %s to %s (type=%s)",
                signal.sender, signal.recipient, signal.type
            )

    def read_signals(self, agent_id: str) -> list[Signal]:
        """
        Read all unread signals addressed to this agent (including broadcasts).
        Marks them as read.
        """
        with self._lock:
            unread = [
                s
                for s in self._signals
                if agent_id not in s.read_by
                and (s.recipient == agent_id or s.recipient == "*")
            ]
            for s in unread:
                s.read_by.add(agent_id)
            logger.debug(
                "Agent %s read %d signals", agent_id, len(unread)
            )
            return unread

    def share_memory(
        self,
        memory: Memory,
        target_team: str | None = None,
    ) -> None:
        """
        Promote a memory to shared space.
        If privacy is PRIVATE, it is upgraded to PUBLIC for sharing.
        """
        if memory.metadata.privacy == PrivacyLevel.PRIVATE:
            memory.metadata.privacy = PrivacyLevel.PUBLIC
        if memory.metadata.privacy == PrivacyLevel.SECRET:
            raise ValueError("Cannot share SECRET memories")

        self._shared.store(memory)
        logger.info(
            "Memory %s shared to team %s", memory.id, target_team
        )

    def get_team_context(self, team_id: str, query: str) -> list[Memory]:
        """Retrieve team-shared memories."""
        return self._shared.search_keywords(query, k=10)

    def register_agent(self, team_id: str, agent_id: str) -> None:
        """Register an agent as part of a team."""
        with self._lock:
            self._agent_scopes[team_id].add(agent_id)

    def get_team_members(self, team_id: str) -> set[str]:
        """List all agents in a team."""
        with self._lock:
            return set(self._agent_scopes.get(team_id, set()))
