"""Unit tests for the stress_test script (StressRunner + helpers)."""

import json
import os
import sys
import tempfile
import unittest

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# Ensure benchmarks package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmarks.stress_test import (
    StressRunner,
    StressReport,
    _make_memory,
    _parse_mix,
    _percentile,
    _get_rss_mb,
)
from uams.storage.memory import InMemoryStore


class TestHelpers(unittest.TestCase):
    def test_percentile_basic(self):
        self.assertEqual(_percentile([1.0], 50), 1.0)
        self.assertEqual(_percentile([1, 2, 3, 4, 5], 50), 3.0)
        self.assertEqual(_percentile([], 50), 0.0)
        # Sorted input: 100 values, p95 ~= 95
        values = list(range(1, 101))
        self.assertAlmostEqual(_percentile(values, 95), 95.05, places=2)

    def test_parse_mix_full(self):
        m = _parse_mix("store,retrieve,search,delete")
        self.assertAlmostEqual(m["store"], 0.50)
        self.assertAlmostEqual(m["retrieve"], 0.30)
        self.assertAlmostEqual(m["search"], 0.15)
        self.assertAlmostEqual(m["delete"], 0.05)
        # Sums to 1.0
        self.assertAlmostEqual(sum(m.values()), 1.0)

    def test_parse_mix_subset_renormalized(self):
        m = _parse_mix("store,retrieve")
        # Sum of fractions for store + retrieve = 0.5 + 0.3 = 0.8
        # After renormalize: store=0.5/0.8=0.625, retrieve=0.3/0.8=0.375
        self.assertAlmostEqual(m["store"], 0.625)
        self.assertAlmostEqual(m["retrieve"], 0.375)

    def test_parse_mix_invalid_op_raises(self):
        with self.assertRaises(ValueError):
            _parse_mix("store,nonsense")

    def test_get_rss_mb_returns_number(self):
        rss = _get_rss_mb()
        # On any platform with the import path available, this is a float >= 0.
        # On sandboxed CI without resource or psutil, it returns 0.0.
        self.assertIsInstance(rss, float)
        self.assertGreaterEqual(rss, 0.0)

    def test_make_memory_returns_valid_memory(self):
        mem = _make_memory("hello world")
        self.assertEqual(mem.payload.raw, "hello world")
        self.assertIn(mem.metadata.memory_type.name,
                      {"WORKING", "EPISODIC", "SEMANTIC"})


class TestStressRunnerInMemory(unittest.TestCase):
    """InMemoryStore is the safe baseline: no network, no locking,
    no FTS5 — so the runner logic itself can be tested deterministically.
    """

    def test_basic_run_completes(self):
        store = InMemoryStore(max_capacity=10000)
        runner = StressRunner(store, ops=200, concurrency=4, mix={
            "store": 0.5, "retrieve": 0.3, "search": 0.15, "delete": 0.05,
        })
        report = runner.run()
        self.assertEqual(report.ops_completed, 200)
        self.assertEqual(report.error_rate, 0.0)
        # ops_per_sec may be 0.0 if elapsed rounds below the timer
        # resolution (e.g. 200 InMemoryStore ops complete in < 1ms).
        # The point is no error, all ops complete, and the report is
        # well-formed. For real timing, run against a real backend.
        self.assertIsInstance(report.ops_per_sec, float)
        self.assertGreaterEqual(report.ops_per_sec, 0.0)

    def test_report_has_per_op_breakdown(self):
        store = InMemoryStore(max_capacity=10000)
        runner = StressRunner(store, ops=200, concurrency=4, mix={
            "store": 0.5, "retrieve": 0.3, "search": 0.15, "delete": 0.05,
        })
        report = runner.run()
        # With 200 ops, all 4 op types should appear (high probability)
        # Store is 50%, retrieve 30% — both should be > 0
        self.assertIn("store", report.by_op)
        self.assertGreater(report.by_op["store"]["n"], 0)
        self.assertIn("retrieve", report.by_op)
        self.assertGreater(report.by_op["retrieve"]["n"], 0)

    def test_report_serializes_to_json(self):
        store = InMemoryStore(max_capacity=10000)
        runner = StressRunner(store, ops=50, concurrency=2, mix={
            "store": 1.0,
        })
        report = runner.run()
        # Round-trip via JSON
        d = json.loads(json.dumps(report.__dict__, default=str))
        self.assertEqual(d["backend"], "")
        self.assertEqual(d["ops_completed"], 50)

    def test_no_warnings_on_clean_run(self):
        store = InMemoryStore(max_capacity=10000)
        runner = StressRunner(store, ops=100, concurrency=2, mix={
            "store": 0.5, "retrieve": 0.3, "search": 0.15, "delete": 0.05,
        })
        report = runner.run()
        # InMemoryStore at 100 ops is trivially fast — no warnings
        self.assertEqual(report.warnings, [])

    def test_concurrency_completes_all_ops(self):
        store = InMemoryStore(max_capacity=10000)
        runner = StressRunner(store, ops=1000, concurrency=16, mix={
            "store": 1.0,
        })
        report = runner.run()
        # Even with 16 threads, all 1000 ops should complete
        self.assertEqual(report.ops_completed, 1000)


if __name__ == "__main__":
    unittest.main()
