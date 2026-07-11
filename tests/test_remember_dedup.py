"""Tests for UniversalMemorySystem.remember() with semantic dedup.

When ``UAMSConfig.remember_dedup_enabled`` is True and an embedding
function is available, remember() should not store a new fact that
is cosine-similar (>= ``remember_dedup_threshold``) to an existing
SEMANTIC memory. Instead, the existing MemoryId is returned. Without
the embedding function, dedup falls back to "always store" with a
debug log.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import UniversalMemorySystem
from uams.config import UAMSConfig
from uams.core.models import AgentContext
from uams.core.enums import MemoryType, PrivacyLevel
from uams.storage.memory import InMemoryStore


# Stable fake embeddings keyed by substring. The keys are checked in
# declaration order, first match wins. This lets us control cosine
# similarity by choosing which key a fact triggers.
_FAKE_EMBEDDINGS = {
    "vegetarian":  [1.0, 0.0, 0.0, 0.0],
    "vegetables":  [0.95, 0.0, 0.31, 0.0],   # ≈ 0.95 cosine to "vegetarian"
    "pizza":       [0.0, 1.0, 0.0, 0.0],       # orthogonal to "vegetarian"
    "italian":     [0.0, 0.0, 1.0, 0.0],      # orthogonal to "pizza" too
    "name alice":  [0.0, 0.0, 0.0, 1.0],      # orthogonal to everything food
    "name alicia": [0.0, 0.0, 0.0, 0.95],     # ≈ 0.95 cosine to "name alice"
}


def _fake_embedding_fn(text: str):
    """Return the first matching fake embedding, or zero vector."""
    for key, vec in _FAKE_EMBEDDINGS.items():
        if key in text.lower():
            return vec
    return [0.0, 0.0, 0.0, 0.0]


def _ctx():
    return AgentContext(agent_id="a1", agent_type="test", session_id="s1")


class TestRememberDedup(unittest.TestCase):

    def test_dedup_disabled_by_default(self):
        """Default config: dedup is off, so the second remember() stores
        a separate memory (current behavior)."""
        cfg = UAMSConfig()
        self.assertFalse(cfg.remember_dedup_enabled)
        ums = UniversalMemorySystem(config=cfg, embedding_fn=_fake_embedding_fn)
        ctx = _ctx()
        id1 = ums.remember("I am vegetarian", ctx)
        id2 = ums.remember("I like vegetables", ctx)
        # Without dedup, two distinct memories
        self.assertNotEqual(id1, id2)
        sem = ums._stores[MemoryType.SEMANTIC]
        self.assertEqual(len(sem.list_all(limit=100)), 2)

    def test_dedup_hits_returns_existing_id(self):
        """Two near-duplicate facts with dedup enabled return the
        SAME MemoryId; only the first is stored."""
        cfg = UAMSConfig(remember_dedup_enabled=True, remember_dedup_threshold=0.90)
        ums = UniversalMemorySystem(config=cfg, embedding_fn=_fake_embedding_fn)
        ctx = _ctx()
        id1 = ums.remember("I am vegetarian", ctx)
        id2 = ums.remember("I like vegetables", ctx)
        # Same id — the second fact was a dedup hit
        self.assertEqual(id1, id2)
        sem = ums._stores[MemoryType.SEMANTIC]
        # Only one memory was actually stored
        self.assertEqual(len(sem.list_all(limit=100)), 1)

    def test_dedup_below_threshold_stores_new(self):
        """Facts with cosine similarity below the threshold are still
        both stored (e.g. "I like pizza" vs "I'm Italian" are related
        but not duplicates)."""
        cfg = UAMSConfig(remember_dedup_enabled=True, remember_dedup_threshold=0.90)
        ums = UniversalMemorySystem(config=cfg, embedding_fn=_fake_embedding_fn)
        ctx = _ctx()
        id1 = ums.remember("I like pizza", ctx)
        id2 = ums.remember("I'm Italian", ctx)
        self.assertNotEqual(id1, id2)
        sem = ums._stores[MemoryType.SEMANTIC]
        self.assertEqual(len(sem.list_all(limit=100)), 2)

    def test_dedup_without_embedding_falls_back_to_store(self):
        """If dedup is enabled but no embedding_fn is available, the
        new fact is stored without dedup (with a debug log)."""
        cfg = UAMSConfig(remember_dedup_enabled=True, remember_dedup_threshold=0.90)
        ums = UniversalMemorySystem(config=cfg, embedding_fn=None)
        ctx = _ctx()
        id1 = ums.remember("I am vegetarian", ctx)
        id2 = ums.remember("I like vegetables", ctx)
        # Two distinct ids — embedding unavailable, both stored
        self.assertNotEqual(id1, id2)
        sem = ums._stores[MemoryType.SEMANTIC]
        self.assertEqual(len(sem.list_all(limit=100)), 2)

    def test_dedup_with_failing_embedding_still_stores(self):
        """If the embedding function raises, the new fact is stored
        without dedup — never break a remember() call."""
        def failing_fn(text):
            raise RuntimeError("embedding service down")
        cfg = UAMSConfig(remember_dedup_enabled=True, remember_dedup_threshold=0.90)
        ums = UniversalMemorySystem(config=cfg, embedding_fn=failing_fn)
        ctx = _ctx()
        id1 = ums.remember("I am vegetarian", ctx)
        id2 = ums.remember("I like vegetables", ctx)
        self.assertNotEqual(id1, id2)

    def test_dedup_threshold_boundary(self):
        """At the boundary threshold, equality is treated as a hit
        (>= threshold matches, per the implementation)."""
        cfg = UAMSConfig(remember_dedup_enabled=True, remember_dedup_threshold=0.95)
        # "vegetarian" and "vegetables" have cosine ≈ 0.95 exactly
        ums = UniversalMemorySystem(config=cfg, embedding_fn=_fake_embedding_fn)
        ctx = _ctx()
        id1 = ums.remember("I am vegetarian", ctx)
        id2 = ums.remember("I like vegetables", ctx)
        self.assertEqual(id1, id2)

    def test_dedup_three_duplicates_only_one_stored(self):
        """Three near-duplicates: only the first is stored; the next
        two return the same id without growing the store."""
        cfg = UAMSConfig(remember_dedup_enabled=True, remember_dedup_threshold=0.90)
        ums = UniversalMemorySystem(config=cfg, embedding_fn=_fake_embedding_fn)
        ctx = _ctx()
        id1 = ums.remember("I am vegetarian", ctx)
        id2 = ums.remember("I like vegetables", ctx)
        id3 = ums.remember("I am a vegetarian", ctx)
        self.assertEqual(id1, id2)
        self.assertEqual(id1, id3)
        sem = ums._stores[MemoryType.SEMANTIC]
        self.assertEqual(len(sem.list_all(limit=100)), 1)


if __name__ == "__main__":
    unittest.main()
