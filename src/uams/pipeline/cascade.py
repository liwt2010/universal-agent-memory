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

    # ------------------------------------------------------------------
    # Discovery helpers (Task 4)
    # ------------------------------------------------------------------

    def _locate_tier(self, memory_id: str) -> Optional[MemoryType]:
        """Find the tier that holds `memory_id`, or None if absent.

        Tries retrieve() on each tier in declaration order. Treats
        exceptions as "not found" so a partially-degraded backend
        doesn't poison the cascade.
        """
        for tier, store in self._stores.items():
            try:
                if store.retrieve(memory_id) is not None:
                    return tier
            except Exception:
                continue
        return None

    def _discover_in_edges(
        self,
        target_id: str,
        tier: MemoryType,
        mode: Optional[str] = None,
    ) -> List[Tuple[str, MemoryType]]:
        """Return list of (source_memory_id, source_tier) referencing target_id.

        Modes (per spec sec 7):
          'scan'  O(N) walk all stores via list_all, filter on relations.
          'index' Use store._reverse_index() if available, else empty.
          'auto'  Try index per-store; fall back to scan per-store.
        """
        mode = mode or self._config.cascade_in_edge_strategy
        results: List[Tuple[str, MemoryType]] = []

        for t, store in self._stores.items():
            if mode == "index":
                rev = getattr(store, "_reverse_index", None)
                if rev:
                    sources = rev.get(target_id) or []
                    results.extend((s, t) for s in sources)
            elif mode == "scan":
                results.extend(self._scan_in_edges_for_store(store, target_id, t))
            elif mode == "auto":
                rev = getattr(store, "_reverse_index", None)
                if rev is not None:
                    sources = rev.get(target_id) or []
                    results.extend((s, t) for s in sources)
                else:
                    results.extend(self._scan_in_edges_for_store(store, target_id, t))
            else:
                raise ValueError(
                    f"Unknown cascade_in_edge_strategy: {mode!r} "
                    "(expected 'scan' | 'index' | 'auto')"
                )
        return results

    def _scan_in_edges_for_store(
        self, store: MemoryStore, target_id: str, tier: MemoryType,
    ) -> List[Tuple[str, MemoryType]]:
        """O(N) scan: list_all then filter relations whose target == target_id."""
        out: List[Tuple[str, MemoryType]] = []
        try:
            iterator = store.list_all(limit=10_000_000)
        except Exception:
            return out
        for mem in iterator:
            for rel in mem.metadata.relations:
                if rel.target_memory_id == target_id:
                    out.append((str(mem.id), tier))
                    break
        return out

    # ------------------------------------------------------------------
    # forget() - three-phase BFS cascade (spec sec 6.1)
    # ------------------------------------------------------------------

    def forget(
        self,
        memory_id: str,
        *,
        strategy=None,                              # CascadeStrategy | str | None
        max_depth: Optional[int] = None,
        in_edge_mode: Optional[str] = None,
    ) -> CascadeReport:
        """Forget a memory and (per strategy) its related memories.

        Three phases:
          1. locate target tier; absent -> audit-only line.
          2. BFS over relations with visit-set + max_depth, same-tier only;
             cross-tier edges added to orphan_ids.
          3. best-effort delete leaves-first; per-memory exceptions
             land in failed_ids. Audit-log line written either way.

        Never raises out of cascade: any exception becomes a
        report entry.
        """
        t0 = time.monotonic()

        # --- normalize inputs ---
        if strategy is None:
            strategy = CascadeStrategy.BIDIRECTIONAL
        if not isinstance(strategy, CascadeStrategy):
            strategy = CascadeStrategy(strategy)
        if max_depth is None:
            max_depth = self._config.cascade_max_depth
        if in_edge_mode is None:
            in_edge_mode = self._config.cascade_in_edge_strategy

        # --- locate target tier ---
        target_tier = self._locate_tier(memory_id)

        report = CascadeReport(
            target_id=memory_id,
            tier=target_tier,
            strategy=strategy,
            audit_log_path=self._audit.path,
        )

        if target_tier is None:
            report.duration_ms = (time.monotonic() - t0) * 1000
            self._audit.append(report.to_dict())
            return report

        target_store = self._stores[target_tier]

        # --- Phase 1: BFS discover ---
        visit_set: Set[str] = {memory_id}
        queue: Deque[Tuple[str, int]] = deque([(memory_id, 0)])
        discovered: List[str] = []

        while queue:
            cur_id, depth = queue.popleft()
            # Stop expanding beyond the depth cap. With max_depth=N we walk
            # N hops from root, processing levels 0..N inclusive (N+1 levels).
            if depth > max_depth:
                continue
            try:
                mem = target_store.retrieve(cur_id)
            except Exception:
                continue
            if mem is None:
                continue
            discovered.append(cur_id)

            if strategy in (CascadeStrategy.OUTGOING, CascadeStrategy.BIDIRECTIONAL):
                for rel in mem.metadata.relations:
                    tgt = rel.target_memory_id
                    if tgt in visit_set:
                        continue
                    tgt_tier = self._locate_tier(tgt)
                    if tgt_tier is None:
                        continue
                    if tgt_tier != target_tier:
                        report.orphan_ids.append((tgt, cur_id))
                        continue
                    visit_set.add(tgt)
                    queue.append((tgt, depth + 1))

            if strategy == CascadeStrategy.BIDIRECTIONAL:
                for src_id, src_tier in self._discover_in_edges(
                    cur_id, target_tier, mode=in_edge_mode,
                ):
                    if src_id in visit_set:
                        continue
                    if src_tier != target_tier:
                        report.orphan_ids.append((src_id, cur_id))
                        continue
                    visit_set.add(src_id)
                    queue.append((src_id, depth + 1))

        # --- Phase 2: best-effort delete (leaves first) ---
        for cid in reversed(discovered):
            try:
                target_store.delete(cid)
                report.deleted_ids.append(cid)
            except Exception as exc:
                report.failed_ids.append((cid, repr(exc)))

        # --- Phase 3: audit ---
        report.duration_ms = (time.monotonic() - t0) * 1000
        self._audit.append(report.to_dict())
        for orphan_id, parent_id in report.orphan_ids:
            self._audit.append_orphan({
                "ts":                    report.to_dict()["ts"],
                "action":                "orphan_edge",
                "orphan_id":             orphan_id,
                "parent_id":             parent_id,
                "triggered_by_target":   memory_id,
                "triggered_by_strategy": strategy.value,
            })
        return report
