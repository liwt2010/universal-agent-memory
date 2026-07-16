"""Forgetting and memory decay engine.

Inspired by Ebbinghaus forgetting curves.
Each memory tier has its own half-life and retention floor, and
per-category overrides can replace the tier default for memories
in that category (e.g. "birthday" never decays, "short_term_preference"
decays in days).
"""

from __future__ import annotations

from datetime import timedelta

from uams.core.enums import MemoryType
from uams.core.models import Memory
from uams.storage.base import MemoryStore


# Sentinel for "never forget": a half-life large enough that
# 0.5 ** (age / NEVER_FORGET) ≈ 1.0 for any realistic age, but
# still small enough to fit in a Python timedelta (which caps
# at 999999999 days ≈ 2.7M years). 10k years is 9 orders of
# magnitude beyond a human-relevant age, so retention at any
# practical age is indistinguishable from 1.0.
NEVER_FORGET_HALF_LIFE_SEC = 10_000 * 365 * 24 * 3600  # 10k years


class ForgettingEngine:
    """
    Ebbinghaus-inspired memory decay.
    Runs as a background task or triggered manually.

    Per-category half-life overrides let operators tune the decay
    curve based on the *meaning* of a memory, not just its tier.
    The tier default applies when no category override matches.
    See `UAMSConfig.category_half_life_overrides` and
    `docs/HALF_LIFE_TUNING.md` for the calibration methodology.
    """

    # (half-life, minimum retention before forgetting)
    DEFAULT_DECAY_CURVES: dict[MemoryType, tuple] = {
        MemoryType.WORKING:    (timedelta(minutes=30), 0.10),
        MemoryType.EPISODIC:   (timedelta(days=7),     0.50),
        MemoryType.SEMANTIC:   (timedelta(days=90),    0.90),
        MemoryType.PROCEDURAL: (timedelta(days=365),   0.95),
    }

    def __init__(
        self,
        stores: dict[MemoryType, MemoryStore],
        decay_curves: dict[MemoryType, tuple] = None,
        category_overrides: dict[str, float | None] = None,
    ):
        """
        :param stores: tier -> MemoryStore
        :param decay_curves: tier -> (half-life, retention_floor).
            Defaults to ``DEFAULT_DECAY_CURVES``.
        :param category_overrides: category string -> half-life in
            seconds. ``None`` value means "never forget" (sentinel
            ``NEVER_FORGET_HALF_LIFE_SEC`` is used internally). When
            a memory has multiple categories, the FIRST matching key
            wins (preserves deterministic, non-additive behavior).
            Empty dict by default; operators MUST populate this from
            observed traffic — the framework cannot guess
            "birthday" vs "short_term_preference" without real data.
        """
        self._stores = stores
        self._decay_curves = decay_curves or self.DEFAULT_DECAY_CURVES
        self._category_overrides = category_overrides or {}

    def _resolve_half_life(self, memory: Memory) -> tuple:
        """Resolve the effective (half-life, retention_floor) for a memory.

        Resolution order:
          1. If a category in ``memory.metadata.categories`` has an
             override in ``category_overrides``, the operator's
             explicit choice wins.
             - ``None`` ("never forget"): use ``NEVER_FORGET_HALF_LIFE_SEC``
               and floor=0.0. With the giant half-life, retention
               ≈ 1.0 forever, so should_forget() never fires.
             - numeric half-life: use the override half-life and
               floor=0.1 (≈ forget after 3-4 halflives with the
               importance/confidence modifiers). The tier's
               stickiness floor is BYPASSED because the operator
               picked a specific rate for a reason; a SEMANTIC
               floor of 0.9 would forget the memory before the
               rate even matters.
          2. Tier default from ``decay_curves`` if no override
             matches.
          3. Hard-coded fallback (30 days, 0.5 floor) if the tier
             itself is missing from ``decay_curves``.
        """
        # 1. Per-category override (first match wins)
        # Iterate the override dict in INSERTION ORDER so the operator's
        # config precedence is honored. (The memory's categories is a
        # set with no guaranteed iteration order, so checking from
        # the memory's side would be non-deterministic.)
        for cat, override_sec in self._category_overrides.items():
            if cat in memory.metadata.categories:
                if override_sec is None:
                    return (
                        timedelta(seconds=NEVER_FORGET_HALF_LIFE_SEC),
                        0.0,
                    )
                # Numeric override: forget after ~3-4 halflives.
                return timedelta(seconds=override_sec), 0.1

        # 2. Tier default
        tier = memory.metadata.memory_type
        return self._decay_curves.get(
            tier, (timedelta(days=30), 0.5)
        )

    def should_forget(self, memory: Memory) -> bool:
        """
        Determine if a memory should be forgotten based on decay curves.

        Returns True if memory should be evicted.
        """
        half_life, retention_floor = self._resolve_half_life(memory)

        age = memory.anchor.age_seconds()
        half_life_sec = half_life.total_seconds()

        # Base exponential decay
        retention = 0.5 ** (age / half_life_sec)

        # Access strengthening: each access boosts retention
        access_count = memory.last_access_count
        retention *= (1.0 + 0.1 * access_count)

        # Importance override: high-importance memories decay slower
        importance_weight = memory.metadata.importance / 10.0
        retention *= (0.5 + 0.5 * importance_weight)

        # Confidence penalty: contradicted memories decay faster
        retention *= memory.metadata.confidence

        return retention < retention_floor

    def sweep(self) -> int:
        """
        Delete expired memories across all tiers.
        Returns count of deleted memories.
        """
        total = 0
        for tier, store in self._stores.items():
            total += store.delete_expired()
        return total

    def evaluate_retention(self, memory: Memory) -> float:
        """
        Calculate the current retention score of a memory (0-1).
        Useful for debugging and visualization.
        """
        half_life, _ = self._resolve_half_life(memory)

        age = memory.anchor.age_seconds()
        half_life_sec = half_life.total_seconds()

        retention = 0.5 ** (age / half_life_sec)
        retention *= (1.0 + 0.1 * memory.last_access_count)
        retention *= (0.5 + 0.5 * memory.metadata.importance / 10.0)
        retention *= memory.metadata.confidence

        return min(retention, 1.0)
