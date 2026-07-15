"""Query rewriting for hybrid retrieval.

Given a user query, expand it into 3-5 variants covering synonyms and
related terms. Each variant is then fed to the standard BM25 + Vector + Graph
retrieval pipeline, and the results are RRF-fused. This improves recall
on short or ambiguous queries (e.g. "Japan hotels?" -> 4 distinct
phrasings that may hit different memory shards).

**Cost / safety**:
- Disabled by default (opt-in via ``UAMSConfig.query_rewrite_enabled``).
- Results are LRU-cached per query string so repeated recalls don't
  burn extra LLM tokens.
- LLM failure -> falls back to ``[query]`` so the main pipeline never stalls.
- Uses a tight timeout (default 5s) and ``max_tokens=128`` per call.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from uams.llm.client import LLMClient, NullLLMClient

logger = logging.getLogger(__name__)


# --- Prompt template (compact; PR1-2-token-opt already trimmed prompts) ---

_REWRITE_SYSTEM = (
    "Expand a search query into 3-5 variants (synonyms, related terms, "
    "rephrasings). Output ONLY the variants, one per line, no numbering or preamble."
)

_REWRITE_USER_TEMPLATE = "Query: {query}\nVariants:"


class QueryRewriter:
    """Optional query-rewriting helper backed by an LLMClient.

    Parameters
    ----------
    llm_client:
        Backing LLM (cheap model recommended, e.g. ``gpt-4o-mini``).
        If ``None`` or ``NullLLMClient``, ``rewrite()`` returns ``[query]``.
    max_variants:
        Hard cap on returned variants. Clamped to ``[1, 8]``.
    timeout:
        LLM call timeout in seconds. Defaults to 5s.
    cache_max_entries:
        LRU cache size for rewrite results. Defaults to 1000.
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        max_variants: int = 4,
        timeout: float = 5.0,
        cache_max_entries: int = 1000,
    ):
        self._llm = llm_client or NullLLMClient()
        self._max_variants = max(1, min(8, int(max_variants)))
        self._timeout = float(timeout)
        self._cache_max = max(1, int(cache_max_entries))
        self._cache: dict = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rewrite(self, query: str) -> list[str]:
        """Return a list of query variants including the original.

        On any error, returns ``[query]`` so the main pipeline never stalls.
        Results are cached by query string.
        """
        query = (query or "").strip()
        if not query:
            return [query] if query else []

        cached = self._cache_get(query)
        if cached is not None:
            return cached

        variants = self._rewrite_uncached(query)
        # Always include the original query as a fallback candidate
        if query not in variants:
            variants = [query] + variants
        # Clamp to max_variants
        variants = variants[: self._max_variants]
        # De-dupe while preserving order
        seen = set()
        deduped = []
        for v in variants:
            v_norm = v.strip()
            if v_norm and v_norm not in seen:
                seen.add(v_norm)
                deduped.append(v_norm)
        self._cache_put(query, deduped)
        return deduped

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rewrite_uncached(self, query: str) -> list[str]:
        """Call the LLM and parse the response. Returns parsed variants
        without the original query (caller adds it).
        """
        try:
            raw = self._llm.chat(
                [
                    {"role": "system", "content": _REWRITE_SYSTEM},
                    {"role": "user", "content": _REWRITE_USER_TEMPLATE.format(query=query)},
                ],
                max_tokens=128,
                temperature=0.0,
                timeout=self._timeout,
            )
        except Exception:
            logger.exception("Query rewrite LLM call failed; falling back to original query")
            return []

        return self._parse_variants(raw)

    @staticmethod
    def _parse_variants(text: str) -> list[str]:
        """Parse the LLM response into a list of variant strings.

        Tolerant of:
        - numbered prefixes ("1. foo", "- foo", "* foo")
        - blank lines
        - surrounding whitespace
        - bullet points
        """
        variants: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Strip leading list markers: "1.", "-", "*", "•", "[1]"
            for prefix in ("- ", "* ", "• "):
                if line.startswith(prefix):
                    line = line[len(prefix):]
                    break
            # Strip leading numbered prefix "1. " or "1) "
            if len(line) > 2 and line[0].isdigit():
                # find the first space or punctuation
                for sep in (". ", ") "):
                    idx = line.find(sep)
                    if 0 < idx <= 3:
                        line = line[idx + len(sep):]
                        break
            # Skip if the line looks like a header echo (the user prompt)
            if line.lower().startswith(("query:", "variants:")):
                continue
            if line:
                variants.append(line)
        return variants

    # ------------------------------------------------------------------
    # Cache helpers (LRU, thread-safe)
    # ------------------------------------------------------------------

    def _cache_key(self, query: str) -> str:
        payload = f"{self._llm.__class__.__name__}|{query}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_get(self, query: str) -> list[str] | None:
        key = self._cache_key(query)
        with self._lock:
            return self._cache.get(key)

    def _cache_put(self, query: str, variants: list[str]) -> None:
        key = self._cache_key(query)
        with self._lock:
            if len(self._cache) >= self._cache_max:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = list(variants)


__all__ = ["QueryRewriter", "_REWRITE_SYSTEM", "_REWRITE_USER_TEMPLATE"]