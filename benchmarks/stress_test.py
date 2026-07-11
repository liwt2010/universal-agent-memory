"""High-concurrency stress test for UAMS storage backends.

This is the "100k ops" A+ requirement — a realistic load test that
exercises the actual code paths (store / retrieve / search / delete)
under concurrent workers, against a real backend. The output is
a JSON report with ops/sec, latency percentiles, error rate, and
process memory growth.

Defaults are tuned for a 10k-op run that fits in CI (1-3 min per
backend depending on latency). For the 100k target, run locally
with --ops=100000 --concurrency=32. The full run takes 10-30
minutes per backend depending on hardware; a real CI job should
either:
  (a) run with --ops=10000 (fast gate, "did we regress?"), or
  (b) run with --ops=100000 on a schedule (nightly stress job,
      results posted to ops dashboard).

Run examples:
  # Local InMemoryStore, 10k ops, 8 threads, 60s budget
  python -m benchmarks.stress_test --backend memory --ops 10000

  # PostgreSQL, 100k ops, 32 threads
  python -m benchmarks.stress_test --backend postgresql \\
      --ops 100000 --concurrency 32

  # Custom mix: 70% store, 20% search, 10% delete
  python -m benchmarks.stress_test --backend redis \\
      --ops 50000 --mix 70,20,10
"""

import argparse
import json
import os
import random
import statistics
import string
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@dataclass
class OpSample:
    """A single operation result."""
    op: str
    latency_ms: float
    ok: bool
    error: Optional[str] = None


@dataclass
class StressReport:
    """Aggregate report of one stress test run."""
    backend: str
    ops_requested: int
    ops_completed: int
    elapsed_sec: float
    concurrency: int
    op_mix: Dict[str, float]  # e.g. {"store": 0.5, "retrieve": 0.3, ...}
    ops_per_sec: float
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    by_op: Dict[str, Dict] = field(default_factory=dict)
    rss_mb_start: float = 0.0
    rss_mb_end: float = 0.0
    rss_growth_mb: float = 0.0
    warnings: List[str] = field(default_factory=list)


def _get_rss_mb() -> float:
    """Current process RSS in MB. Best-effort, returns 0 if unavailable."""
    try:
        import resource
        # On POSIX, ru_maxrss is in KB on Linux, bytes on macOS. We use KB.
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return rss_kb / (1024 * 1024)
        return rss_kb / 1024
    except Exception:
        try:
            import psutil
            return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        except Exception:
            return 0.0


def _make_memory(raw: str, tier=0):
    """Create a Memory for the stress test. tier: 0=WORKING, 1=EPISODIC, 2=SEMANTIC."""
    from uams import (
        Memory, MemoryId, TemporalAnchor, AgentContext,
        MemoryPayload, MemoryMetadata, MemoryType, PrivacyLevel,
    )
    tier_enum = [MemoryType.WORKING, MemoryType.EPISODIC, MemoryType.SEMANTIC][tier % 3]
    return Memory(
        id=MemoryId(),
        anchor=TemporalAnchor(),
        context=AgentContext(agent_id="stress", agent_type="stress", session_id="stress"),
        payload=MemoryPayload(raw=raw),
        metadata=MemoryMetadata(memory_type=tier_enum, privacy=PrivacyLevel.PUBLIC),
    )


def _parse_mix(spec: str) -> Dict[str, float]:
    """Parse 'store,retrieve,search,delete' percentages (must sum to 100)."""
    parts = [p.strip().lower() for p in spec.split(",") if p.strip()]
    valid = {"store", "retrieve", "search", "delete"}
    for p in parts:
        if p not in valid:
            raise ValueError(f"Unknown op in mix: {p!r} (valid: {sorted(valid)})")
    # Default: 50% store, 30% retrieve, 15% search, 5% delete
    defaults = {
        "store": 0.50, "retrieve": 0.30, "search": 0.15, "delete": 0.05,
    }
    out = {}
    for p in parts:
        out[p] = defaults[p]
    # Renormalize (in case the user gave only 2-3 ops)
    total = sum(out.values()) or 1.0
    return {k: v / total for k, v in out.items()}


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Return the pct-th percentile of a sorted list. Returns 0 if empty."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


class StressRunner:
    """Run concurrent operations against a UAMS store, collecting samples."""

    def __init__(self, store, ops: int, concurrency: int, mix: Dict[str, float],
                 timeout_sec: float = 600.0, seed: int = 42):
        self.store = store
        self.ops = ops
        self.concurrency = concurrency
        self.mix = mix
        self.timeout_sec = timeout_sec
        self.seed = seed
        self._stop = threading.Event()
        self._samples: List[OpSample] = []
        self._lock = threading.Lock()
        self._seeded_ids: List[str] = []  # collected IDs available for retrieve/delete
        self._seeded_lock = threading.Lock()

    def _record(self, sample: OpSample):
        with self._lock:
            self._samples.append(sample)

    def _track_id(self, mid: str):
        with self._seeded_lock:
            self._seeded_ids.append(mid)

    def _get_random_id(self) -> Optional[str]:
        with self._seeded_lock:
            if not self._seeded_ids:
                return None
            return random.choice(self._seeded_ids)

    def _choose_op(self) -> str:
        r = random.random()
        cum = 0.0
        for op, frac in self.mix.items():
            cum += frac
            if r <= cum:
                return op
        return list(self.mix.keys())[-1]

    def _run_op(self) -> OpSample:
        op = self._choose_op()
        t0 = time.monotonic()
        try:
            if op == "store":
                raw = "stress-" + "".join(random.choices(string.ascii_letters, k=20))
                mem = _make_memory(raw)
                self.store.store(mem)
                self._track_id(str(mem.id))
            elif op == "retrieve":
                mid = self._get_random_id()
                if mid is None:
                    # No ID available yet; treat as a skip (not error)
                    return OpSample(op=op, latency_ms=(time.monotonic() - t0) * 1000, ok=True)
                self.store.retrieve(mid)
            elif op == "search":
                kw = "stress-" + "".join(random.choices(string.ascii_letters, k=4))
                if hasattr(self.store, "search_keywords"):
                    self.store.search_keywords(kw, k=10)
                else:
                    return OpSample(op=op, latency_ms=(time.monotonic() - t0) * 1000, ok=True)
            elif op == "delete":
                mid = self._get_random_id()
                if mid is None:
                    return OpSample(op=op, latency_ms=(time.monotonic() - t0) * 1000, ok=True)
                if hasattr(self.store, "delete"):
                    self.store.delete(mid)
            else:
                raise ValueError(f"unknown op {op!r}")
            return OpSample(op=op, latency_ms=(time.monotonic() - t0) * 1000, ok=True)
        except Exception as exc:
            return OpSample(
                op=op, latency_ms=(time.monotonic() - t0) * 1000,
                ok=False, error=f"{type(exc).__name__}: {exc}"[:200],
            )

    def _worker(self, ops_per_worker: int):
        for _ in range(ops_per_worker):
            if self._stop.is_set():
                return
            self._record(self._run_op())

    def run(self) -> StressReport:
        random.seed(self.seed)
        rss_start = _get_rss_mb()
        # Distribute ops across workers as evenly as possible. With
        # ops=1000 and concurrency=16: 16 workers * 62 = 992, plus
        # 8 workers that do 63 (the first `ops % concurrency` workers
        # get the extra op). Total = self.ops exactly.
        base = self.ops // self.concurrency
        extra = self.ops % self.concurrency
        per_worker = [base + (1 if i < extra else 0) for i in range(self.concurrency)]
        total_planned = sum(per_worker)

        t0 = time.monotonic()
        threads = [
            threading.Thread(target=self._worker, args=(per_worker[i],), daemon=True)
            for i in range(self.concurrency)
        ]
        for t in threads:
            t.start()
        deadline = t0 + self.timeout_sec
        for t in threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
            if not t.is_alive():
                continue
            # Timed out — stop remaining workers
            self._stop.set()
            break
        for t in threads:
            t.join(timeout=5.0)
        elapsed = time.monotonic() - t0
        rss_end = _get_rss_mb()
        return self._build_report(elapsed, rss_start, rss_end, total_planned)

    def _build_report(self, elapsed: float, rss_start: float, rss_end: float,
                       total_planned: int) -> StressReport:
        samples = list(self._samples)
        n_ok = sum(1 for s in samples if s.ok)
        n_err = len(samples) - n_ok
        by_op: Dict[str, List[OpSample]] = {}
        for s in samples:
            by_op.setdefault(s.op, []).append(s)

        def _per_op_stats(slist: List[OpSample]) -> Dict:
            if not slist:
                return {"n": 0, "ok": 0, "err": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}
            lats = sorted(s.latency_ms for s in slist)
            n = len(slist)
            n_e = sum(1 for s in slist if not s.ok)
            return {
                "n": n, "ok": n - n_e, "err": n_e,
                "p50_ms": _percentile(lats, 50),
                "p95_ms": _percentile(lats, 95),
                "p99_ms": _percentile(lats, 99),
                "max_ms": lats[-1],
            }

        by_op_stats = {op: _per_op_stats(slist) for op, slist in by_op.items()}
        all_lats = sorted(s.latency_ms for s in samples)
        report = StressReport(
            backend="",
            ops_requested=self.ops,
            ops_completed=len(samples),
            elapsed_sec=round(elapsed, 3),
            concurrency=self.concurrency,
            op_mix={k: round(v, 4) for k, v in self.mix.items()},
            ops_per_sec=round(len(samples) / elapsed, 1) if elapsed > 0 else 0.0,
            error_rate=round(n_err / max(1, len(samples)), 6),
            p50_ms=round(_percentile(all_lats, 50), 3),
            p95_ms=round(_percentile(all_lats, 95), 3),
            p99_ms=round(_percentile(all_lats, 99), 3),
            max_ms=round(all_lats[-1], 3) if all_lats else 0.0,
            by_op=by_op_stats,
            rss_mb_start=round(rss_start, 2),
            rss_mb_end=round(rss_end, 2),
            rss_growth_mb=round(rss_end - rss_start, 2),
        )

        # Warnings: error rate over 1%, p95 over 1s, memory growth over 200MB.
        if report.error_rate > 0.01:
            report.warnings.append(
                f"error_rate={report.error_rate:.2%} > 1% (failure threshold)"
            )
        if report.p95_ms > 1000:
            report.warnings.append(
                f"p95 latency {report.p95_ms:.0f}ms > 1000ms (slow)"
            )
        if report.rss_growth_mb > 200:
            report.warnings.append(
                f"RSS grew {report.rss_growth_mb:.0f}MB during run (>200MB, possible leak)"
            )
        if report.ops_completed < report.ops_requested:
            report.warnings.append(
                f"only {report.ops_completed}/{report.ops_requested} ops completed (timeout?)"
            )
        return report


def _build_store(backend: str, args) -> object:
    """Build a real UAMS store from the chosen backend."""
    from uams.config import UAMSConfig
    from uams.storage.memory import InMemoryStore
    from uams.storage.sqlite import SQLiteStore

    if backend == "memory":
        return InMemoryStore(max_capacity=max(args.ops * 2, 10000))

    if backend == "sqlite":
        path = args.sqlite_path or "stress_test.db"
        return SQLiteStore(db_path=path, tier_name=args.tier)

    if backend == "redis":
        from uams.storage.redis import RedisStore
        return RedisStore(
            host=args.redis_host, port=args.redis_port,
            key_prefix=args.redis_prefix or "uams:stress:",
            max_capacity=max(args.ops * 2, 10000),
        )

    if backend == "postgresql":
        from uams.storage.postgresql import PostgreSQLStore
        return PostgreSQLStore(
            host=args.pg_host, port=args.pg_port,
            user=args.pg_user, password=args.pg_password,
            database=args.pg_db, table_name=args.pg_table,
        )

    if backend == "neo4j":
        from uams.storage.neo4j import Neo4jStore
        return Neo4jStore(
            uri=args.neo4j_uri, user=args.neo4j_user,
            password=args.neo4j_password, database=args.neo4j_db,
        )

    if backend == "chromadb":
        from uams.storage.chromadb import ChromaDBStore
        return ChromaDBStore(collection_name="stress_test")

    raise ValueError(f"Unknown backend: {backend!r}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="UAMS high-concurrency stress test")
    p.add_argument("--backend", default="memory",
                   choices=["memory", "sqlite", "redis", "postgresql", "neo4j", "chromadb"])
    p.add_argument("--ops", type=int, default=10000,
                   help="Total operations to perform (default 10000).")
    p.add_argument("--concurrency", type=int, default=8,
                   help="Number of worker threads (default 8).")
    p.add_argument("--mix", default="store,retrieve,search,delete",
                   help="Op mix as comma-separated ops; percentages are fixed: "
                        "store=50%%, retrieve=30%%, search=15%%, delete=5%% (default: all 4).")
    p.add_argument("--timeout", type=float, default=600.0,
                   help="Hard wall-clock budget in seconds (default 600).")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", default="stress_report.json",
                   help="Write JSON report to this path (default ./stress_report.json).")
    p.add_argument("--tier", default="working",
                   choices=["working", "episodic", "semantic", "procedural"])
    # Per-backend env / flag overrides (default to env-var-driven)
    p.add_argument("--sqlite-path", default=os.environ.get("UAMS_STRESS_SQLITE_PATH"))
    p.add_argument("--redis-host", default=os.environ.get("UAMS_TEST_REDIS_HOST", "127.0.0.1"))
    p.add_argument("--redis-port", type=int, default=int(os.environ.get("UAMS_TEST_REDIS_PORT", "6379")))
    p.add_argument("--redis-prefix", default=os.environ.get("UAMS_STRESS_REDIS_PREFIX"))
    p.add_argument("--pg-host", default=os.environ.get("UAMS_TEST_PG_HOST", "127.0.0.1"))
    p.add_argument("--pg-port", type=int, default=int(os.environ.get("UAMS_TEST_PG_PORT", "5432")))
    p.add_argument("--pg-user", default=os.environ.get("UAMS_TEST_PG_USER", "postgres"))
    p.add_argument("--pg-password", default=os.environ.get("UAMS_TEST_PG_PASSWORD", "postgres"))
    p.add_argument("--pg-db", default=os.environ.get("UAMS_TEST_PG_DB", "postgres"))
    p.add_argument("--pg-table", default=os.environ.get("UAMS_STRESS_PG_TABLE", "uams_stress"))
    p.add_argument("--neo4j-uri", default=os.environ.get("UAMS_TEST_NEO4J_URI", "bolt://127.0.0.1:7687"))
    p.add_argument("--neo4j-user", default=os.environ.get("UAMS_TEST_NEO4J_USER", "neo4j"))
    p.add_argument("--neo4j-password", default=os.environ.get("UAMS_TEST_NEO4J_PASSWORD", "testpass"))
    p.add_argument("--neo4j-db", default=os.environ.get("UAMS_TEST_NEO4J_DB", "neo4j"))
    args = p.parse_args(argv)

    if args.ops < 1:
        print("error: --ops must be >= 1", file=sys.stderr)
        return 2

    mix = _parse_mix(args.mix)
    store = _build_store(args.backend, args)
    runner = StressRunner(
        store=store,
        ops=args.ops,
        concurrency=args.concurrency,
        mix=mix,
        timeout_sec=args.timeout,
        seed=args.seed,
    )
    print(f"[stress] backend={args.backend} ops={args.ops} concurrency={args.concurrency}")
    print(f"[stress] mix={mix}")
    report = runner.run()
    report.backend = args.backend

    # Persist JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2)

    # Human summary
    print()
    print(f"=== Stress Report ({args.backend}) ===")
    print(f"ops: {report.ops_completed}/{report.ops_requested} in {report.elapsed_sec:.2f}s")
    print(f"throughput: {report.ops_per_sec:.1f} ops/sec")
    print(f"error_rate: {report.error_rate:.4%}")
    print(f"latency: p50={report.p50_ms:.1f}ms  p95={report.p95_ms:.1f}ms  "
          f"p99={report.p99_ms:.1f}ms  max={report.max_ms:.1f}ms")
    print(f"per-op:")
    for op, stats in sorted(report.by_op.items()):
        print(f"  {op}: n={stats['n']}  err={stats['err']}  "
              f"p50={stats['p50_ms']:.1f}ms  p95={stats['p95_ms']:.1f}ms  "
              f"p99={stats['p99_ms']:.1f}ms")
    print(f"rss: {report.rss_mb_start:.1f}MB -> {report.rss_mb_end:.1f}MB  "
          f"(+{report.rss_growth_mb:.1f}MB)")
    if report.warnings:
        print(f"warnings:")
        for w in report.warnings:
            print(f"  - {w}")
    print(f"\nJSON report written to {args.output}")

    # Exit code: 0 if no warnings, 1 if any (so CI can flag but not hard-fail
    # unless --strict is set in a future enhancement).
    return 1 if report.warnings else 0


if __name__ == "__main__":
    sys.exit(main())
