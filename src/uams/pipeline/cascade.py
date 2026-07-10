"""Cross-layer cascade deletion for memory forget (GDPR-friendly)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

from uams.config import UAMSConfig
from uams.core.enums import MemoryType
from uams.storage.base import MemoryStore


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class CascadeStrategy(str, Enum):
    """Cascade behavior when forgetting a memory.

    Inheriting `str` makes instances JSON-serializable without a custom encoder.
    """
    ISOLATED = "isolated"
    OUTGOING = "outgoing"
    BIDIRECTIONAL = "bidirectional"


@dataclass
class CascadeReport:
    """Outcome of a `CascadeForgetter.forget()` invocation."""
    target_id: str
    tier: Optional[MemoryType]
    strategy: CascadeStrategy

    deleted_ids: List[str] = field(default_factory=list)
    orphan_ids:  List[Tuple[str, str]] = field(default_factory=list)
    failed_ids:  List[Tuple[str, str]] = field(default_factory=list)

    duration_ms: float = 0.0
    audit_log_path: Optional[Path] = None

    @property
    def deleted_count(self) -> int: return len(self.deleted_ids)

    @property
    def orphan_count(self) -> int:  return len(self.orphan_ids)

    @property
    def failed_count(self) -> int:  return len(self.failed_ids)

    @property
    def is_complete(self) -> bool:  return self.failed_count == 0

    def to_dict(self) -> dict:
        return {
            "ts":            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "action":        "cascade_forget",
            "target_id":     self.target_id,
            "tier":          self.tier.name if self.tier is not None else None,
            "strategy":      self.strategy.value,
            "deleted_count": self.deleted_count,
            "orphan_count":  self.orphan_count,
            "failed_count":  self.failed_count,
            "deleted_ids":   list(self.deleted_ids),
            "orphan_ids":    [list(p) for p in self.orphan_ids],
            "failed_ids":    [list(p) for p in self.failed_ids],
            "duration_ms":   self.duration_ms,
            "is_complete":   self.is_complete,
        }


# ---------------------------------------------------------------------------
# CascadeForgetter - implementation lands in Task 4+
# ---------------------------------------------------------------------------

class CascadeForgetter:
    """Cascade-deleting forgetter. Best-effort, audit-logged, BFS-bounded."""

    def __init__(
        self,
        stores: Dict[MemoryType, MemoryStore],
        config: UAMSConfig,
        audit_writer,            # CascadeAuditWriter - local import avoided for cycle safety
    ) -> None:
        self._stores = stores
        self._config = config
        self._audit = audit_writer

    def forget(
        self,
        memory_id: str,
        *,
        strategy=None,           # CascadeStrategy | str | None
        max_depth: Optional[int] = None,
        in_edge_mode: Optional[str] = None,
    ):
        """Placeholder - full implementation lands in Task 5."""
        raise NotImplementedError("Task 5 will implement.")
