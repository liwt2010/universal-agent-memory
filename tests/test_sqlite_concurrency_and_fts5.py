"""Tests for the 2 pre-existing bugs found during the 7-11 review pass.

Bug 1: SQLite pool_size 5 was tight under 4+ concurrent write threads
        (WAL mode serializes writes, so pool got starved of available
         connections and busy_timeout retries slowed things down).
        Fix: default pool 5 -> 8 + RLock around write paths + busy_timeout=5000.

Bug 2: FTS5 MATCH treats '-' as NOT operator, so search_keywords('state-of-the-art')
        returned empty results.
        Fix: wrap query as FTS5 phrase ("...") to treat it as a literal string.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.core.enums import MemoryType, PrivacyLevel
from uams.core.models import (
    AgentContext,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    TemporalAnchor,
)
from uams.storage.sqlite import SQLiteStore


def _make_memory(raw: str) -> Memory:
    return Memory(
        id=MemoryId(),
        anchor=TemporalAnchor(),
        context=AgentContext("a", "t", "s"),
        payload=MemoryPayload(raw=raw),
        metadata=MemoryMetadata(MemoryType.WORKING, PrivacyLevel.PUBLIC),
    )


class TestSQLiteConcurrentWrites(unittest.TestCase):
    """Bug 1: 4+ threads writing concurrently should not raise SQLITE_BUSY
    or deadlock. RLock serializes writes; pool of 8 covers 1 writer + readers.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "concurrent.db")
        self.store = SQLiteStore(self.db_path, "concurrent", pool_size=8)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_4_threads_50_writes_each_no_errors(self):
        """4 threads x 50 writes = 200 concurrent stores. All must succeed."""
        errors = []
        n_threads = 4
        n_per_thread = 50
        barrier = threading.Barrier(n_threads)

        def worker(thread_id: int):
            try:
                barrier.wait(timeout=5)
                for i in range(n_per_thread):
                    mem = _make_memory(f"thread{thread_id}_item{i}")
                    self.store.store(mem)
            except Exception as e:  # noqa: BLE001
                errors.append(f"thread{thread_id}: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], f"Concurrent writes failed: {errors}")
        all_mems = self.store.list_all(limit=1000)
        self.assertEqual(len(all_mems), n_threads * n_per_thread)

    def test_8_threads_20_writes_each_completes_quickly(self):
        """8 threads x 20 writes = 160 writes. Should finish in <30s (no deadlock)."""
        errors = []
        n_threads = 8
        n_per_thread = 20
        barrier = threading.Barrier(n_threads)

        def worker(thread_id: int):
            try:
                barrier.wait(timeout=5)
                for i in range(n_per_thread):
                    mem = _make_memory(f"t{thread_id}_i{i}")
                    self.store.store(mem)
            except Exception as e:  # noqa: BLE001
                errors.append(f"thread{thread_id}: {type(e).__name__}: {e}")

        start = time.time()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        elapsed = time.time() - start

        self.assertEqual(errors, [], f"Concurrent writes failed: {errors}")
        self.assertLess(elapsed, 30.0, f"Took {elapsed:.1f}s, expected <30s")

    def test_mixed_reads_and_writes_dont_deadlock(self):
        """2 writers + 4 readers mixed workload should not deadlock or error."""
        errors = []
        n_writers = 2
        n_readers = 4
        n_per = 30
        barrier = threading.Barrier(n_writers + n_readers)

        def writer(thread_id: int):
            try:
                barrier.wait(timeout=5)
                for i in range(n_per):
                    mem = _make_memory(f"writer{thread_id}_item{i}")
                    self.store.store(mem)
            except Exception as e:  # noqa: BLE001
                errors.append(f"writer{thread_id}: {type(e).__name__}: {e}")

        def reader(thread_id: int):
            try:
                barrier.wait(timeout=5)
                for i in range(n_per):
                    self.store.list_all(limit=10)
            except Exception as e:  # noqa: BLE001
                errors.append(f"reader{thread_id}: {type(e).__name__}: {e}")

        threads = []
        for i in range(n_writers):
            threads.append(threading.Thread(target=writer, args=(i,)))
        for i in range(n_readers):
            threads.append(threading.Thread(target=reader, args=(i,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], f"Mixed read/write failed: {errors}")


class TestSQLiteFTS5PhraseQuery(unittest.TestCase):
    """Bug 2: FTS5 MATCH should treat user queries as literal phrases,
    not as FTS5 query syntax. Hyphen, asterisk, colon etc. should be safe.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "fts5.db")
        self.store = SQLiteStore(self.db_path, "fts5test", pool_size=5)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_hyphen_phrase_finds_results(self):
        """'state-of-the-art' used to be parsed as 'state AND NOT of AND NOT ...'
        returning empty. After fix, it should find the literal phrase."""
        self.store.store(_make_memory("the state-of-the-art approach is great"))
        self.store.store(_make_memory("plain text with no hyphens"))
        results = self.store.search_keywords("state-of-the-art", k=5)
        self.assertEqual(len(results), 1)
        self.assertIn("state-of-the-art", results[0].payload.raw)

    def test_asterisk_in_query_does_not_break_syntax(self):
        """'*' is FTS5 prefix wildcard. Should be treated as literal char."""
        self.store.store(_make_memory("file with * wildcard chars"))
        results = self.store.search_keywords("*", k=5)
        # "*" alone as a phrase: the FTS5 tokenizer will split on punctuation,
        # so this may return 0 — that's fine, what matters is no exception.
        # The key fix is that the * doesn't crash or get interpreted as prefix op.
        self.assertIsInstance(results, list)

    def test_embedded_double_quote_in_query_escaped(self):
        """Query containing '"' should be escaped, not break phrase syntax."""
        self.store.store(_make_memory('he said "hello world" yesterday'))
        results = self.store.search_keywords('said "hello world"', k=5)
        self.assertEqual(len(results), 1)
        self.assertIn("hello world", results[0].payload.raw)

    def test_single_word_query_unchanged(self):
        """Regression: simple single-word queries must still work."""
        self.store.store(_make_memory("apple banana cherry"))
        self.store.store(_make_memory("grape peach"))
        results = self.store.search_keywords("apple", k=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload.raw, "apple banana cherry")

    def test_multi_word_phrase_query(self):
        """Multi-word phrase should be found via FTS5 phrase match."""
        self.store.store(_make_memory("machine learning is fun"))
        self.store.store(_make_memory("deep learning is powerful"))
        results = self.store.search_keywords("machine learning", k=5)
        self.assertGreater(len(results), 0)
        # The first-ranked result should be the exact phrase
        self.assertEqual(results[0].payload.raw, "machine learning is fun")

    def test_sanitize_fts5_query_helper(self):
        """Unit test for the _sanitize_fts5_query helper."""
        # Simple case
        self.assertEqual(SQLiteStore._sanitize_fts5_query("hello"), '"hello"')
        # Hyphens preserved
        self.assertEqual(
            SQLiteStore._sanitize_fts5_query("state-of-the-art"),
            '"state-of-the-art"',
        )
        # Embedded quotes doubled
        self.assertEqual(
            SQLiteStore._sanitize_fts5_query('say "hi"'),
            '"say ""hi"""',
        )
        # Empty
        self.assertEqual(SQLiteStore._sanitize_fts5_query(""), '""')


if __name__ == "__main__":
    unittest.main()
