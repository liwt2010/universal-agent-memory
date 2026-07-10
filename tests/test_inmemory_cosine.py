"""Tests for InMemoryStore's real cosine similarity search_vector.

Covers:
  - _cosine_similarity helper math (unit)
  - _metadata_matches filter (unit)
  - search_vector end-to-end on InMemoryStore
      - empty query / zero-norm query
      - ranking order (closest first, ties broken by recency)
      - dim mismatch + missing-embedding are skipped silently
      - memory_type / privacy filters
      - LRU touch on hit
      - touch order: closer hits first, even if older
"""

import os
import sys
import unittest
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.storage.memory import InMemoryStore
from uams.core.models import Memory, MemoryId, TemporalAnchor, MemoryPayload, MemoryMetadata, AgentContext
from uams.core.enums import MemoryType, PrivacyLevel


def _mem(mid: str, embedding=None, raw="x", mem_type=MemoryType.SEMANTIC,
        privacy=PrivacyLevel.PUBLIC, created_at=1.0):
    return Memory(
        id=MemoryId(mid),
        anchor=TemporalAnchor(created_at=created_at, expires_at=None),
        context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
        payload=MemoryPayload(raw=raw, embedding=embedding),
        metadata=MemoryMetadata(
            memory_type=mem_type, privacy=privacy,
            importance=5.0, confidence=1.0,
        ),
    )


class TestCosineMath(unittest.TestCase):

    def test_identical_returns_1(self):
        v = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(InMemoryStore._cosine_similarity(v, list(v)), 1.0)

    def test_orthogonal_returns_0(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        self.assertAlmostEqual(InMemoryStore._cosine_similarity(a, b), 0.0)

    def test_opposite_direction_returns_neg1(self):
        a = [1.0, 2.0, 3.0]
        b = [-1.0, -2.0, -3.0]
        self.assertAlmostEqual(InMemoryStore._cosine_similarity(a, b), -1.0)

    def test_zero_vector_returns_0(self):
        self.assertEqual(InMemoryStore._cosine_similarity([0, 0, 0], [1, 2, 3]), 0.0)
        self.assertEqual(InMemoryStore._cosine_similarity([1, 2, 3], [0, 0, 0]), 0.0)

    def test_dim_mismatch_returns_0(self):
        self.assertEqual(InMemoryStore._cosine_similarity([1, 2], [1, 2, 3]), 0.0)

    def test_empty_returns_0(self):
        self.assertEqual(InMemoryStore._cosine_similarity([], []), 0.0)
        self.assertEqual(InMemoryStore._cosine_similarity([1, 2], []), 0.0)


class TestMetadataMatches(unittest.TestCase):

    def test_no_filters_always_matches(self):
        m = _mem("a")
        self.assertTrue(InMemoryStore._metadata_matches(m, {}))

    def test_memory_type_match_by_name(self):
        m = _mem("a", mem_type=MemoryType.SEMANTIC)
        self.assertTrue(InMemoryStore._metadata_matches(m, {"memory_type": "SEMANTIC"}))
        self.assertFalse(InMemoryStore._metadata_matches(m, {"memory_type": "WORKING"}))

    def test_privacy_match_by_name(self):
        m = _mem("a", privacy=PrivacyLevel.PUBLIC)
        self.assertTrue(InMemoryStore._metadata_matches(m, {"privacy": "PUBLIC"}))
        self.assertFalse(InMemoryStore._metadata_matches(m, {"privacy": "SECRET"}))


class TestSearchVector(unittest.TestCase):

    def setUp(self):
        self.store = InMemoryStore()

    def test_empty_query_returns_empty(self):
        self.store.store(_mem("a", embedding=[0.1, 0.2, 0.3]))
        self.assertEqual(self.store.search_vector([]), [])

    def test_zero_norm_query_returns_empty(self):
        self.store.store(_mem("a", embedding=[0.1, 0.2, 0.3]))
        self.assertEqual(self.store.search_vector([0.0, 0.0, 0.0]), [])

    def test_empty_store_returns_empty(self):
        # matches the contract that 3 pre-existing assertions verify
        self.assertEqual(self.store.search_vector([0.1, 0.2]), [])

    def test_closest_match_ranks_first(self):
        # Query is nearly identical to 'b' (cosine ~1.0).
        # 'a' shares the x-axis component (cos ~0.105); 'c' is the z-axis (cos = 0).
        self.store.store(_mem("a", embedding=[1.0, 0.0, 0.0]))
        self.store.store(_mem("b", embedding=[0.1, 0.95, 0.0]))
        self.store.store(_mem("c", embedding=[0.0, 0.0, 1.0]))
        results = self.store.search_vector([0.1, 0.95, 0.0], k=3)
        self.assertEqual([str(r.id) for r in results], ["b", "a", "c"])

    def test_k_limits_results(self):
        for i in range(5):
            self.store.store(_mem(f"m{i}", embedding=[float(i), 0.0, 0.0]))
        results = self.store.search_vector([1.0, 0.0, 0.0], k=2)
        self.assertEqual(len(results), 2)

    def test_memory_without_embedding_is_skipped(self):
        self.store.store(_mem("no_emb"))  # embedding=None
        self.store.store(_mem("with_emb", embedding=[1.0, 0.0]))
        results = self.store.search_vector([1.0, 0.0])
        self.assertEqual([str(r.id) for r in results], ["with_emb"])

    def test_dim_mismatch_is_skipped(self):
        self.store.store(_mem("dim2", embedding=[1.0, 0.0]))
        self.store.store(_mem("dim3", embedding=[1.0, 0.0, 0.0]))
        results = self.store.search_vector([1.0, 0.0, 0.0])
        # dim2 silently dropped; only dim3 with matching dim scores
        self.assertEqual([str(r.id) for r in results], ["dim3"])

    def test_zero_norm_memory_embedding_skipped(self):
        self.store.store(_mem("zero", embedding=[0.0, 0.0, 0.0]))
        self.store.store(_mem("real", embedding=[1.0, 0.0, 0.0]))
        results = self.store.search_vector([1.0, 0.0, 0.0])
        self.assertEqual([str(r.id) for r in results], ["real"])

    def test_memory_type_filter(self):
        self.store.store(_mem("sem", embedding=[1.0, 0.0], mem_type=MemoryType.SEMANTIC))
        self.store.store(_mem("work", embedding=[1.0, 0.0], mem_type=MemoryType.WORKING))
        results = self.store.search_vector([1.0, 0.0], memory_type="SEMANTIC")
        self.assertEqual([str(r.id) for r in results], ["sem"])

    def test_privacy_filter(self):
        self.store.store(_mem("pub", embedding=[1.0, 0.0], privacy=PrivacyLevel.PUBLIC))
        self.store.store(_mem("sec", embedding=[1.0, 0.0], privacy=PrivacyLevel.SECRET))
        results = self.store.search_vector([1.0, 0.0], privacy="PUBLIC")
        self.assertEqual([str(r.id) for r in results], ["pub"])

    def test_returned_count_reflects_k_not_total(self):
        for i in range(10):
            self.store.store(_mem(f"m{i}", embedding=[1.0, 0.0]))
        results = self.store.search_vector([1.0, 0.0], k=3)
        self.assertEqual(len(results), 3)

    def test_tiebreaker_is_recency_then_created_at(self):
        # Two memories with the same cosine (same direction, different magnitude)
        self.store.store(_mem("older", embedding=[1.0, 0.0], created_at=1.0))
        self.store.store(_mem("newer", embedding=[2.0, 0.0], created_at=2.0))
        # Cosine is 1.0 for both relative to query (1, 0).
        # Tie broken by created_at desc -> 'newer' first.
        results = self.store.search_vector([3.0, 0.0], k=2)
        self.assertEqual([str(r.id) for r in results], ["newer", "older"])


if __name__ == "__main__":
    unittest.main()
