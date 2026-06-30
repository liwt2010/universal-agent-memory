"""Forgetting and memory decay engine.

Inspired by Ebbinghaus forgetting curves.
Each memory tier has its own half-life and retention floor.
"""

import math
from datetime import timedelta
from typing import Dict

from uams.core.enums import MemoryType
from uams.core.models import Memory
from uams.storage.base import MemoryStore


class ForgettingEngine:
    """
    Ebbinghaus-inspired memory decay.
    Runs as a background task or triggered manually.
    """

    # (half-life, minimum retention before forgetting)
    DEFAULT_DECAY_CURVES: Dict[MemoryType, tuple] = {
        MemoryType.WORKING:    (timedelta(minutes=30), 0.10),
        MemoryType.EPISODIC:   (timedelta(days=7),     0.50),
        MemoryType.SEMANTIC:   (timedelta(days=90),    0.90),
        MemoryType.PROCEDURAL: (timedelta(days=365),   0.95),
    }

    def __init__(
        self,
        stores: Dict[MemoryType, MemoryStore],
        decay_curves: Dict[MemoryType, tuple] = None,
    ):
        self._stores = stores
        self._decay_curves = decay_curves or self.DEFAULT_DECAY_CURVES

    def should_forget(self, memory: Memory) -> bool:
        """
        Determine if a memory should be forgotten based on decay curves.

        Returns True if memory should be evicted.
        """
        tier = memory.metadata.memory_type
        half_life, retention_floor = self._decay_curves.get(
            tier, (timedelta(days=30), 0.5)
        )

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
        tier = memory.metadata.memory_type
        half_life, _ = self._decay_curves.get(tier, (timedelta(days=30), 0.5))

        age = memory.anchor.age_seconds()
        half_life_sec = half_life.total_seconds()

        retention = 0.5 ** (age / half_life_sec)
        retention *= (1.0 + 0.1 * memory.last_access_count)
        retention *= (0.5 + 0.5 * memory.metadata.importance / 10.0)
        retention *= memory.metadata.confidence

        return min(retention, 1.0)
