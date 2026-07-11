# Half-Life Tuning Guide

`UAMSConfig.category_half_life_overrides` lets you replace a tier's
default half-life on a per-category basis. It is **empty by default**
and **must be populated from observed traffic** — the framework cannot
guess how fast "birthday" vs "short_term_preference" should decay
without real data.

This guide is the methodology, not a default values table. The values
you ship are yours.

## Mechanism

```python
from uams.config import UAMSConfig
from uams.core.enums import MemoryType, PrivacyLevel

cfg = UAMSConfig(
    semantic_half_life_seconds=90 * 86400,   # tier default for SEMANTIC
    category_half_life_overrides={
        "birthday":          None,            # None = never forget
        "long_term_pref":    365 * 86400,    # 1 year
        "short_term_pref":   3 * 86400,      # 3 days
        "transient_context": 6 * 3600,       # 6 hours
    },
)
```

When a memory has multiple categories, the **first matching key in
the override dict** (in insertion order) wins. The memory's
`categories` is a set with no guaranteed iteration order, so we
iterate the override dict instead — the operator's config
precedence is honored.

When a category has no override, the tier's default applies:

| Tier     | Default half-life | Default retention_floor |
|----------|-------------------|-------------------------|
| WORKING  | 30 min            | 0.10                    |
| EPISODIC | 7 days            | 0.50                    |
| SEMANTIC | 90 days           | 0.90                    |
| PROCEDURAL | 365 days        | 0.95                    |

## The floor override

When a category override applies, the engine uses **floor=0.1**
(forget after ~3-4 halflives, with importance/confidence modifiers)
instead of the tier's stickiness floor. The rationale: the
operator picked a specific rate for a reason; the tier's
"stubborn" floor is the *implicit* default that the override
explicitly opts out of. Otherwise a 3-day override on a SEMANTIC
memory (tier floor 0.9) would be forgotten before the rate even
matters.

The "never forget" case (`None` value) is special: floor=0.0,
half-life=10k years. Retention stays at ~1.0 forever, so
`should_forget()` never fires.

## Why calibration matters

A "wrong" half-life has two failure modes:

1. **Too long**: low-value memories accumulate, dilute retrieval,
   waste storage. The user sees a noisy `recall()`.
2. **Too short**: useful memories disappear, the user has to
   re-state the same fact. "Why doesn't it remember my birthday?"

The right value is workload-dependent:

- A personal-assistant that handles a user's restaurant
  preferences daily has very different traffic than a one-off
  research agent that handles millions of distinct users.
- A B2B customer-support memory system has different
  half-life economics than a personal-assistant one.
- The "freshness vs. stickiness" tradeoff shifts with the
  retrieval budget and the noise tolerance of the user.

Hardcoded values from a different workload are wrong for yours.

## Calibration methodology

1. **Instrument first.** Log the `Memory.metadata.categories` of
   every `remember()` and the access pattern (count + last
   access time) of every `recall()` hit. Without this, you are
   guessing.

2. **Bucket by category.** Group memories by category and look
   at two distributions per bucket:
   - **Access age**: how long after creation do users still
     look up a memory in this category? The 90th percentile is
     a candidate half-life. (If 90% of accesses happen within
     3 days, the half-life is ~1 day.)
   - **Re-statement rate**: how often does the user re-state
     something we already have? High re-statement rate = the
     half-life is too short.

3. **Start conservative.** Ship values 2-3x longer than your
   measured half-life. The cost of "kept a bit too long" is
   cheap (extra storage + occasional noisy recall); the cost
   of "forgot something important" is high (user re-statement,
   lost context).

4. **A/B test the floor.** The 0.1 floor is a "forget after 3-4
   halflives" heuristic. If your users complain that memories
   are forgotten too aggressively, raise the floor (e.g. 0.05
   = forget after 4-5 halflives). The floor is a constant in
   `ForgettingEngine._resolve_half_life` for now; exposing it
   as a config field is a future change.

5. **Watch for "long tail" categories.** A new deployment
   starts with no overrides, so every memory uses the tier
   default. As you discover new categories (e.g. a new
   workflow produces memories tagged "session_summary"),
   add them to the override map. Do not let the override
   dict rot.

## What this is NOT

- **Not a hard-delete policy.** `should_forget()` returns a
  boolean; the actual deletion happens in `ForgettingEngine.sweep()`,
  which calls `store.delete_expired()`. The override here affects
  the *retention* curve, not the *eviction* path.

- **Not per-user.** The override dict is global. Per-user
  half-life tuning is a future extension (the `Memory.context`
  carries `user_id`, so a per-user lookup is a small change).

- **Not auto-tuned.** The framework does not learn optimal
  half-lives from access patterns. That's a feedback loop that
  needs careful guarding (you don't want a runaway feedback
  loop that "forgets everything" because of a misconfigured
  retrieval). v0.4+ is a candidate for an offline batch tuner
  that ingests logs and proposes overrides for human review.

## Reference implementation

- `src/uams/pipeline/forgetting.py` — `ForgettingEngine._resolve_half_life`
- `src/uams/config.py` — `category_half_life_overrides` field
- `tests/test_category_half_life.py` — 10 tests covering precedence,
  fallback, `None` sentinel, and back-compat with the existing
  tier-default behavior
