"""Cross-layer cascade deletion for memory forget (GDPR-friendly)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Deque

from uams.config import UAMSConfig
from uams.core.enums import MemoryType
from uams.storage.base import MemoryStore
from uams.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class CascadeStrategy(str, Enum):
    """Cascade behavior when forgetting a memory.

    Inheriting `str` makes instances JSON-serializable without a custom encoder.

    Strategies differ in **scope** (which directions to follow) and
    **strictness** (whether cross-tier edges block or get followed):

    - ISOLATED       : delete target only
    - OUTGOING       : delete target + out-edges (BFS), same-tier only
    - BIDIRECTIONAL  : delete target + out-edges + in-edges (BFS), same-tier only;
                       cross-tier edges recorded as `orphan_ids` (NOT deleted).
                       **Default.** Aligns with "right to be forgotten" within
                       a single storage tier (e.g. delete the user's session
                       in Episodic, leave the related facts in Semantic for
                       a separate decision).
    - FULL_CASCADE   : **explicit opt-in only.** delete target + out-edges +
                       in-edges across all tiers. Cross-tier edges ARE
                       deleted (not orphan). The audit log still records
                       every deletion with its tier, so the trail is
                       intact. Use this when a user invokes GDPR Article 17
                       and wants the data gone from every storage layer
                       UAMS owns, not just the originating tier.
    """
    ISOLATED = "isolated"
    OUTGOING = "outgoing"
    BIDIRECTIONAL = "bidirectional"
    FULL_CASCADE = "full_cascade"


@dataclass
class CascadeReport:
    """Outcome of a `CascadeForgetter.forget()` invocation."""
    target_id: str
    tier: MemoryType | None
    strategy: CascadeStrategy

    deleted_ids: list[str] = field(default_factory=list)
    # In FULL_CASCADE mode, cross-tier edges that were also deleted
    # are recorded here as (id, original_tier) so the operator can see
    # exactly which memories left the system and from which storage layer.
    # This is the GDPR-friendly "right to be forgotten" trail.
    cross_tier_deleted_ids: list[tuple[str, str]] = field(default_factory=list)
    orphan_ids:  list[tuple[str, str]] = field(default_factory=list)
    failed_ids:  list[tuple[str, str]] = field(default_factory=list)

    duration_ms: float = 0.0
    audit_log_path: Path | None = None

    @property
    def deleted_count(self) -> int: return len(self.deleted_ids)

    @property
    def cross_tier_deleted_count(self) -> int: return len(self.cross_tier_deleted_ids)

    @property
    def orphan_count(self) -> int:  return len(self.orphan_ids)

    @property
    def failed_count(self) -> int:  return len(self.failed_ids)

    @property
    def is_complete(self) -> bool:  return self.failed_count == 0

    def to_dict(self) -> dict:
        return {
            "ts":                      datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "action":                  "cascade_forget",
            "target_id":               self.target_id,
            "tier":                    self.tier.name if self.tier is not None else None,
            "strategy":                self.strategy.value,
            "deleted_count":           self.deleted_count,
            "cross_tier_deleted_count": self.cross_tier_deleted_count,
            "orphan_count":            self.orphan_count,
            "failed_count":            self.failed_count,
            "deleted_ids":             list(self.deleted_ids),
            "cross_tier_deleted_ids":  [list(p) for p in self.cross_tier_deleted_ids],
            "orphan_ids":              [list(p) for p in self.orphan_ids],
            "failed_ids":              [list(p) for p in self.failed_ids],
            "duration_ms":             self.duration_ms,
            "is_complete":             self.is_complete,
        }


# ---------------------------------------------------------------------------
# CascadeForgetter - implementation lands in Task 4+
# ---------------------------------------------------------------------------

class CascadeForgetter:
    """Cascade-deleting forgetter. Best-effort, audit-logged, BFS-bounded."""

    def __init__(
        self,
        stores: dict[MemoryType, MemoryStore],
        config: UAMSConfig,
        audit_writer,            # CascadeAuditWriter - local import avoided for cycle safety
    ) -> None:
        self._stores = stores
        self._config = config
        self._audit = audit_writer

    # ------------------------------------------------------------------
    # Discovery helpers (Task 4)
    # ------------------------------------------------------------------

    def _locate_tier(self, memory_id: str) -> MemoryType | None:
        """Find the tier that holds `memory_id`, or None if absent.

        v0.6.x: delegates to ``store.find_tier`` on every store.
        Each store does one targeted lookup (O(1) on stores with
        a primary-key index, O(matches) on the in-memory fallback).
        Errors are logged at ERROR and treated as not-found so a
        partially-degraded backend doesn't poison the cascade.
        """
        for tier, store in self._stores.items():
            try:
                if store.find_tier(memory_id):
                    return tier
            except Exception as exc:
                logger.error(
                    "cascade._locate_tier: tier=%s retrieve(%s) raised; "
                    "treating as not-found",
                    tier.name, memory_id, exc_info=True,
                )
                continue
        return None

    def _discover_in_edges(
        self,
        target_id: str,
        tier: MemoryType,
        mode: str | None = None,
    ) -> list[tuple[str, MemoryType]]:
        """Return list of (source_memory_id, source_tier) referencing target_id.

        Modes (per spec sec 7):
          'scan'  O(N) walk all stores via in_edges_scan.
          'index' Use store.in_edges() per-store. Stores that don't
                  maintain a reverse index (has_reverse_index=False)
                  return [] — index mode is strict-by-default, the
                  operator has opted into the index path.
          'auto'  Per-store: try in_edges() first if the store has
                  a reverse index; fall back to in_edges_scan().

        v0.6.x: routes through the new ``store.in_edges()`` /
        ``store.in_edges_scan()`` / ``store.has_reverse_index``
        MemoryStore API. Stores that maintain a reverse index
        (InMemoryStore, SQLiteStore) get O(1) per in-edge query.
        Other stores fall back to the O(N) scan automatically in
        'auto' mode.
        """
        mode = mode or self._config.cascade_in_edge_strategy
        results: list[tuple[str, MemoryType]] = []

        for t, store in self._stores.items():
            if mode == "index":
                # Strict: only use the maintained reverse index.
                # Stores without one return [] (per the base
                # MemoryStore.in_edges default).
                sources = store.in_edges(target_id)
                results.extend((s, t) for s in sources)
            elif mode == "scan":
                results.extend(
                    (s, t) for s in store.in_edges_scan(target_id)
                )
            elif mode == "auto":
                if getattr(store, "has_reverse_index", False):
                    sources = store.in_edges(target_id)
                    if sources:
                        results.extend((s, t) for s in sources)
                    else:
                        # Has an index but the target has no
                        # in-edges here. Trust the index.
                        pass
                else:
                    results.extend(
                        (s, t) for s in store.in_edges_scan(target_id)
                    )
            else:
                raise ValueError(
                    f"Unknown cascade_in_edge_strategy: {mode!r} "
                    "(expected 'scan' | 'index' | 'auto')"
                )
        return results

    def _scan_in_edges_for_store(
        self, store: MemoryStore, target_id: str, tier: MemoryType,
    ) -> list[tuple[str, MemoryType]]:
        """O(N) scan: list_all then filter relations whose target == target_id."""
        out: list[tuple[str, MemoryType]] = []
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
        max_depth: int | None = None,
        in_edge_mode: str | None = None,
    ) -> CascadeReport:
        """Forget a memory and (per strategy) its related memories.

        Three phases:
          1. locate target tier; absent -> audit-only line.
          2. BFS over relations with visit-set + max_depth. In
             ISOLATED / OUTGOING / BIDIRECTIONAL strategies, cross-tier
             edges are recorded as `orphan_ids` (not deleted). In
             FULL_CASCADE (explicit opt-in), cross-tier edges are
             followed: the discovered memory is deleted from its own
             tier and the cross-tier deletion is recorded in
             `cross_tier_deleted_ids` for the audit trail.
          3. best-effort delete leaves-first across whatever tiers the
             BFS discovered; per-memory exceptions land in `failed_ids`.
             Audit-log line written either way.

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

        # FULL_CASCADE follows cross-tier edges; everything else stops
        # at the tier boundary and orphans the foreign memory.
        follows_cross_tier = strategy == CascadeStrategy.FULL_CASCADE

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
        # Queue carries (id, tier, depth) so the loop can dispatch
        # retrieve/delete to the correct store when FULL_CASCADE lets
        # us cross tier boundaries.
        visit_set: set[str] = {memory_id}
        queue: Deque[tuple[str, MemoryType, int]] = deque([(memory_id, target_tier, 0)])
        # (id, originating_tier) — same-tier items have tier == target_tier;
        # cross-tier items carry the tier they were discovered in, so the
        # delete phase hits the right store and the audit records the tier.
        discovered: list[tuple[str, MemoryType]] = []

        while queue:
            cur_id, cur_tier, depth = queue.popleft()
            # Stop expanding beyond the depth cap. With max_depth=N we walk
            # N hops from root, processing levels 0..N inclusive (N+1 levels).
            if depth > max_depth:
                continue
            cur_store = self._stores[cur_tier]
            try:
                mem = cur_store.retrieve(cur_id)
            except Exception:
                continue
            if mem is None:
                continue
            discovered.append((cur_id, cur_tier))

            if strategy in (CascadeStrategy.OUTGOING, CascadeStrategy.BIDIRECTIONAL, CascadeStrategy.FULL_CASCADE):
                for rel in mem.metadata.relations:
                    tgt = rel.target_memory_id
                    if tgt in visit_set:
                        continue
                    tgt_tier = self._locate_tier(tgt)
                    if tgt_tier is None:
                        continue
                    if tgt_tier != cur_tier:
                        if follows_cross_tier:
                            # FULL_CASCADE: follow the cross-tier edge.
                            # The delete phase will hit the right store
                            # because we record tgt_tier alongside tgt.
                            visit_set.add(tgt)
                            queue.append((tgt, tgt_tier, depth + 1))
                        else:
                            # Other strategies: orphan, don't follow.
                            report.orphan_ids.append((tgt, cur_id))
                        continue
                    visit_set.add(tgt)
                    queue.append((tgt, tgt_tier, depth + 1))

            if strategy in (CascadeStrategy.BIDIRECTIONAL, CascadeStrategy.FULL_CASCADE):
                for src_id, src_tier in self._discover_in_edges(
                    cur_id, cur_tier, mode=in_edge_mode,
                ):
                    if src_id in visit_set:
                        continue
                    if src_tier != cur_tier:
                        if follows_cross_tier:
                            visit_set.add(src_id)
                            queue.append((src_id, src_tier, depth + 1))
                        else:
                            report.orphan_ids.append((src_id, cur_id))
                        continue
                    visit_set.add(src_id)
                    queue.append((src_id, src_tier, depth + 1))

        # --- Phase 2: best-effort delete (leaves first) ---
        # Iterate in reverse-discovered order so leaves are deleted
        # before their parents (less likely to fail on a partial
        # cleanup). For cross-tier items, dispatch to the originating
        # store and record in `cross_tier_deleted_ids`.
        for cid, cid_tier in reversed(discovered):
            cid_store = self._stores[cid_tier]
            try:
                cid_store.delete(cid)
                report.deleted_ids.append(cid)
                if cid_tier != target_tier:
                    report.cross_tier_deleted_ids.append((cid, cid_tier.name))
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
