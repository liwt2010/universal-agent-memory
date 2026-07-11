"""Tests for ForgettingEngine per-category half-life overrides."""

import os
import sys
import time
import unittest

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.pipeline.forgetting import ForgettingEngine, NEVER_FORGET_HALF_LIFE_SEC
from uams.core.enums import MemoryType, PrivacyLevel
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata,
)


def _make(cats, days_old, tier=MemoryType.SEMANTIC, importance=5.0, confidence=0.95):
    return Memory(
        id=MemoryId("x"),
        anchor=TemporalAnchor(created_at=time.time() - 86400 * days_old),
        context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
        payload=MemoryPayload(raw="x"),
        metadata=MemoryMetadata(
            memory_type=tier,
            categories=set(cats),
            privacy=PrivacyLevel.PUBLIC,
            importance=importance,
            confidence=confidence,
        ),
    )


class TestCategoryOverride(unittest.TestCase):
    """Per-category half-life overrides. The mechanism is opt-in: an
    empty override dict behaves like before. Operators populate it
    based on observed traffic — there are no built-in defaults.
    """

    def test_no_override_uses_tier_default(self):
        """No overrides configured -> tier default half-life wins."""
        eng = ForgettingEngine({})
        m = _make({"general"}, days_old=30)
        hl, floor = eng._resolve_half_life(m)
        # SEMANTIC default is 90 days
        self.assertEqual(hl.days, 90)
        # SEMANTIC default floor is 0.9
        self.assertEqual(floor, 0.9)

    def test_none_override_means_never_forget(self):
        """A ``None`` value in the override dict means "never decay"."""
        eng = ForgettingEngine({}, category_overrides={"birthday": None})
        m = _make({"birthday"}, days_old=365 * 100)  # 100 years old
        # should_forget is False at any age
        self.assertFalse(eng.should_forget(m))
        # Half-life is the sentinel
        hl, _ = eng._resolve_half_life(m)
        self.assertEqual(hl.total_seconds(), NEVER_FORGET_HALF_LIFE_SEC)

    def test_numeric_override_changes_half_life(self):
        """A numeric value replaces the tier's half-life."""
        eng = ForgettingEngine({}, category_overrides={"short_term": 3 * 86400})
        m = _make({"short_term"}, days_old=30)
        hl, _ = eng._resolve_half_life(m)
        self.assertEqual(hl.days, 3)
        # At 14d, past ~4 halflives, retention is very low
        m_old = _make({"short_term"}, days_old=14)
        self.assertTrue(eng.should_forget(m_old))
        # At 1d, retention still > floor
        m_fresh = _make({"short_term"}, days_old=1)
        self.assertFalse(eng.should_forget(m_fresh))

    def test_first_matching_category_wins(self):
        """When a memory has multiple categories, the first matching
        key in the override DICT (in insertion order) wins. This
        makes the precedence deterministic and operator-controlled.
        """
        eng = ForgettingEngine(
            {},
            category_overrides={
                "alpha": 86400,            # 1 day
                "beta":  None,             # never forget
                "gamma": 30 * 86400,       # 30 days
            },
        )
        # alpha is the first key in the dict, so it wins regardless
        # of the memory's category-set iteration order.
        m_with_alpha = _make({"alpha", "beta", "gamma"}, days_old=10)
        hl, _ = eng._resolve_half_life(m_with_alpha)
        self.assertEqual(hl.total_seconds(), 86400)  # alpha wins

        # Without alpha, beta wins (next key in the dict)
        m_without_alpha = _make({"beta", "gamma"}, days_old=10)
        hl, _ = eng._resolve_half_life(m_without_alpha)
        self.assertEqual(hl.total_seconds(), NEVER_FORGET_HALF_LIFE_SEC)  # beta wins

        # Operator can change precedence by reordering the dict
        eng_reversed = ForgettingEngine(
            {},
            category_overrides={
                "beta":  None,             # never forget
                "alpha": 86400,            # 1 day
                "gamma": 30 * 86400,
            },
        )
        hl, _ = eng_reversed._resolve_half_life(m_with_alpha)
        self.assertEqual(hl.total_seconds(), NEVER_FORGET_HALF_LIFE_SEC)  # beta now first

    def test_unknown_category_falls_back_to_tier(self):
        """A memory with no category matching an override uses the
        tier default."""
        eng = ForgettingEngine({}, category_overrides={"birthday": None})
        m = _make({"random_category"}, days_old=30)
        hl, floor = eng._resolve_half_life(m)
        self.assertEqual(hl.days, 90)  # SEMANTIC default
        self.assertEqual(floor, 0.9)

    def test_override_bypasses_tier_stickiness_floor(self):
        """Numeric override sets floor to 0.1 (forget after ~3-4
        halflives), not the tier's stickiness floor (e.g. 0.9 for
        SEMANTIC). The operator picked a specific rate; the tier's
        stickiness knob is the implicit default that overrides opt
        out of."""
        eng = ForgettingEngine({}, category_overrides={"short_term": 3 * 86400})
        m = _make({"short_term"}, days_old=10)
        _, floor = eng._resolve_half_life(m)
        # Floor is 0.1, NOT 0.9 (SEMANTIC's tier default)
        self.assertEqual(floor, 0.1)
        self.assertNotEqual(floor, 0.9)

    def test_numeric_override_uses_consistent_should_forget(self):
        """should_forget respects the override end-to-end."""
        eng = ForgettingEngine({}, category_overrides={"short_term": 3 * 86400})
        # 1d old: retention ≈ 0.6, not yet forgotten
        self.assertFalse(eng.should_forget(_make({"short_term"}, days_old=1)))
        # 3d old: retention ≈ 0.4, not yet forgotten (floor 0.1)
        self.assertFalse(eng.should_forget(_make({"short_term"}, days_old=3)))
        # 10d old: retention ≈ 0.04, well below floor 0.1
        self.assertTrue(eng.should_forget(_make({"short_term"}, days_old=10)))

    def test_should_forget_short_term_with_zero_importance(self):
        """Importance 0 + low confidence can drive retention below
        0.1 quickly even at low age, which is the expected behavior
        (low-importance + low-confidence memories decay faster)."""
        eng = ForgettingEngine({}, category_overrides={"short_term": 3 * 86400})
        m = _make({"short_term"}, days_old=2, importance=0.1, confidence=0.1)
        # importance_factor ≈ 0.5, confidence 0.1 -> retention = 0.5^0.67 * 0.505 * 0.1 ≈ 0.04
        self.assertTrue(eng.should_forget(m))


class TestOverridePreservesExistingBehavior(unittest.TestCase):
    """Backward-compat: engines with no override dict behave exactly
    as before. These tests would fail if the new mechanism broke
    the existing decay logic.
    """

    def test_default_engine_uses_all_tiers(self):
        eng = ForgettingEngine({})
        for tier, expected_hl_seconds in [
            (MemoryType.WORKING, 30 * 60),       # 30 min
            (MemoryType.EPISODIC, 7 * 86400),
            (MemoryType.SEMANTIC, 90 * 86400),
            (MemoryType.PROCEDURAL, 365 * 86400),
        ]:
            m = _make({"general"}, days_old=1, tier=tier)
            hl, _ = eng._resolve_half_life(m)
            self.assertEqual(
                hl.total_seconds(), expected_hl_seconds,
                f"tier {tier.name} should have {expected_hl_seconds}s half-life, got {hl.total_seconds()}",
            )

    def test_empty_override_dict_same_as_none(self):
        """Passing an empty dict is equivalent to passing nothing."""
        eng_a = ForgettingEngine({})
        eng_b = ForgettingEngine({}, category_overrides={})
        m = _make({"general"}, days_old=30)
        self.assertEqual(
            eng_a._resolve_half_life(m),
            eng_b._resolve_half_life(m),
        )


if __name__ == "__main__":
    unittest.main()
