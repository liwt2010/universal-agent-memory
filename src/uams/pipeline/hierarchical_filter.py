"""Hierarchical filter for LLM-based compression.

Two-stage pre-filter that runs **before** the LLM is called, so the
expensive summarization call receives fewer, higher-signal events:

L1 — Structural filtering (rule-based, no LLM):
  - Drop events with very short content (< ``min_content_length``)
  - Drop ``ENV_OBSERVATION`` events that have no ``structured_data``
  - De-duplicate events with identical content (keep the first)

L2 — Keyword extraction (TF-IDF-lite, no LLM):
  - Tokenize the surviving events, drop common English stop words,
    rank by frequency, return top-K tokens
  - The keywords are prepended to the user prompt as a "Key terms:"
    hint so the LLM pays attention to high-signal words

Both stages are opt-in and gracefully degrade: if no events survive L1,
L3 (the LLM call) still runs on the original list.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, List, Set

from uams.core.enums import EventType
from uams.core.models import AgentEvent


# Conservative English stop-word set. Keeps the implementation small
# without pulling in NLTK. Add domain-specific stop words as needed.
_STOP_WORDS: Set[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "can", "shall",
        "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
        "us", "them", "my", "your", "his", "its", "our", "their",
        "this", "that", "these", "those", "of", "in", "on", "at", "to",
        "for", "with", "by", "from", "as", "into", "about", "between",
        "through", "before", "after", "above", "below", "is", "are",
        "was", "were", "be", "been", "being", "am",
    }
)

# Match word characters only; lowercased; length >= 2
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")


class HierarchicalFilter:
    """L1 filter + L2 keyword extraction, both LLM-free."""

    def __init__(
        self,
        min_content_length: int = 5,
        drop_observation_only: bool = True,
        keyword_top_k: int = 10,
    ):
        self._min_len = max(1, int(min_content_length))
        self._drop_obs = bool(drop_observation_only)
        self._top_k = max(0, int(keyword_top_k))

    # ------------------------------------------------------------------
    # L1: structural filter
    # ------------------------------------------------------------------

    def filter_events(self, events: List[AgentEvent]) -> List[AgentEvent]:
        """Return a deduplicated, low-quality-filtered subset of events.

        Preserves original order. If no events survive, returns the
        original list (so the LLM still has something to summarize).
        """
        if not events:
            return events

        out: List[AgentEvent] = []
        seen_contents: Set[str] = set()
        for ev in events:
            content = (ev.content or "").strip()
            # Rule 1: too short
            if len(content) < self._min_len:
                continue
            # Rule 2: pure observation without structured data
            if (
                self._drop_obs
                and ev.event_type == EventType.ENV_OBSERVATION
                and not ev.structured_data
            ):
                continue
            # Rule 3: de-dupe identical content
            if content in seen_contents:
                continue
            seen_contents.add(content)
            out.append(ev)

        # Fallback: if the filter removed everything, hand back the originals
        # so the LLM still has data to work with.
        return out if out else list(events)

    # ------------------------------------------------------------------
    # L2: keyword extraction
    # ------------------------------------------------------------------

    def extract_keywords(self, events: Iterable[AgentEvent]) -> List[str]:
        """Return the top-K most-frequent non-stopword tokens from the events."""
        if self._top_k == 0:
            return []
        counter: Counter = Counter()
        for ev in events:
            for token in self._tokenize(ev.content or ""):
                counter[token] += 1
        # Counter.most_common is already sorted by (count desc, insertion order)
        return [w for w, _ in counter.most_common(self._top_k)]

    def keyword_hint(self, events: Iterable[AgentEvent]) -> str:
        """Return a "Key terms: ..." hint string for the LLM prompt, or "" if none."""
        kws = self.extract_keywords(events)
        if not kws:
            return ""
        return "Key terms: " + ", ".join(kws) + "\n"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _tokenize(self, text: str) -> List[str]:
        out: List[str] = []
        for m in _TOKEN_RE.finditer(text):
            tok = m.group(0).lower()
            if tok in _STOP_WORDS:
                continue
            if len(tok) < 2:
                continue
            out.append(tok)
        return out


__all__ = ["HierarchicalFilter", "_STOP_WORDS", "_TOKEN_RE"]