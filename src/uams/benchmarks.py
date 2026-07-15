"""Performance benchmark suite for UAMS.

Measures key operations under various loads and memory sizes.
Run with: python -m benchmarks.run
"""

from __future__ import annotations

import time
import random
import string
from typing import Any

from uams import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata, MemoryType, PrivacyLevel,
)
from uams.storage.memory import InMemoryStore
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class BenchmarkResult:
    """Result of a single benchmark run."""

    def __init__(self, name: str, elapsed: float, ops: int, details: dict[str, Any] = None):
        self.name = name
        self.elapsed = elapsed
        self.ops = ops
        self.details = details or {}
        self.ops_per_sec = ops / elapsed if elapsed > 0 else 0

    def __repr__(self) -> str:
        return f"BenchmarkResult({self.name}: {self.ops_per_sec:.1f} ops/sec, {self.elapsed:.3f}s for {self.ops} ops)"


class BenchmarkSuite:
    """Suite of performance benchmarks for UAMS components."""

    @staticmethod
    def _random_text(length: int = 100) -> str:
        return ''.join(random.choices(string.ascii_letters + ' ', k=length))

    @staticmethod
    def _make_memory(raw: str) -> Memory:
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="bench", agent_type="benchmark", session_id="bench"),
            payload=MemoryPayload(raw=raw),
            metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
        )

    @classmethod
    def benchmark_store(cls, n: int = 1000) -> BenchmarkResult:
        """Benchmark store() operation."""
        store = InMemoryStore(max_capacity=n + 100)
        start = time.time()
        for i in range(n):
            mem = cls._make_memory(cls._random_text(50))
            store.store(mem)
        elapsed = time.time() - start
        return BenchmarkResult("store", elapsed, n)

    @classmethod
    def benchmark_retrieve(cls, n: int = 1000) -> BenchmarkResult:
        """Benchmark retrieve() operation."""
        store = InMemoryStore(max_capacity=n + 100)
        ids = []
        for i in range(n):
            mem = cls._make_memory(cls._random_text(50))
            store.store(mem)
            ids.append(str(mem.id))
        start = time.time()
        for mid in ids:
            store.retrieve(mid)
        elapsed = time.time() - start
        return BenchmarkResult("retrieve", elapsed, n)

    @classmethod
    def benchmark_search_keywords(cls, n_memories: int = 1000, n_queries: int = 50) -> BenchmarkResult:
        """Benchmark keyword search."""
        store = InMemoryStore(max_capacity=n_memories + 100)
        words = ["apple", "banana", "cherry", "date", "elderberry"]
        for i in range(n_memories):
            raw = f"{random.choice(words)} {cls._random_text(30)}"
            store.store(cls._make_memory(raw))
        start = time.time()
        for _ in range(n_queries):
            store.search_keywords(random.choice(words), k=10)
        elapsed = time.time() - start
        return BenchmarkResult("search_keywords", elapsed, n_queries, {"n_memories": n_memories})

    @classmethod
    def benchmark_delete_expired(cls, n: int = 1000) -> BenchmarkResult:
        """Benchmark delete_expired() with mixed expired/fresh memories."""
        import time as time_mod
        store = InMemoryStore(max_capacity=n + 100)
        now = time_mod.time()
        for i in range(n):
            expires = now - 1 if i % 2 == 0 else now + 1000
            mem = Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(created_at=now, expires_at=expires),
                context=AgentContext(agent_id="bench", agent_type="benchmark", session_id="bench"),
                payload=MemoryPayload(raw=cls._random_text(20)),
                metadata=MemoryMetadata(memory_type=MemoryType.WORKING, privacy=PrivacyLevel.PUBLIC),
            )
            store.store(mem)
        start = time.time()
        count = store.delete_expired()
        elapsed = time.time() - start
        return BenchmarkResult("delete_expired", elapsed, n, {"deleted": count})

    @classmethod
    def run_all(cls, n: int = 1000) -> list[BenchmarkResult]:
        """Run all benchmarks and return results."""
        logger.info("Starting UAMS benchmark suite (n=%d)...", n)
        results = []
        results.append(cls.benchmark_store(n))
        results.append(cls.benchmark_retrieve(n))
        results.append(cls.benchmark_search_keywords(n, n_queries=max(50, n // 20)))
        results.append(cls.benchmark_delete_expired(n))
        logger.info("Benchmark suite completed")
        return results

    @classmethod
    def print_report(cls, results: list[BenchmarkResult]) -> str:
        """Generate a formatted benchmark report."""
        lines = ["\n" + "=" * 60, "UAMS Benchmark Report", "=" * 60]
        for r in results:
            lines.append(f"{r.name:25s} {r.ops_per_sec:10.1f} ops/sec  ({r.elapsed:.3f}s for {r.ops} ops)")
            if r.details:
                for k, v in r.details.items():
                    lines.append(f"  └─ {k}: {v}")
        lines.append("=" * 60 + "\n")
        report = "\n".join(lines)
        logger.info(report)
        return report


def main():
    """Run benchmarks from command line."""
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    results = BenchmarkSuite.run_all(n)
    BenchmarkSuite.print_report(results)


if __name__ == "__main__":
    main()
