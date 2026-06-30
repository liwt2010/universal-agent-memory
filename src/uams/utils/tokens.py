"""Token estimation utilities for UAMS.

Provides accurate token counts using tiktoken (if available) or
cheap character-based heuristics as fallback.
Supports English, Chinese, and mixed text.
"""

from typing import List, Optional

from uams.utils.logging import get_logger

logger = get_logger(__name__)


class TokenEstimator:
    """
    Estimates token counts for text.
    Uses tiktoken (OpenAI) if available, else falls back to heuristic.
    """

    def __init__(self, model: str = "gpt-4"):
        self._model = model
        self._encoder = None
        try:
            import tiktoken
            self._encoder = tiktoken.encoding_for_model(model)
            logger.info("Using tiktoken encoder for model %s", model)
        except ImportError:
            logger.warning("tiktoken not installed. Using heuristic token estimation.")
        except Exception as e:
            logger.warning("Failed to load tiktoken encoder: %s. Using heuristic.", e)

    def estimate(self, text: str) -> int:
        """Return estimated token count for the given text."""
        if self._encoder:
            try:
                return len(self._encoder.encode(text))
            except Exception:
                logger.debug("tiktoken encoding failed, falling back to heuristic")
        return self._heuristic_estimate(text)

    def estimate_batch(self, texts: List[str]) -> List[int]:
        if self._encoder:
            try:
                return [len(self._encoder.encode(t)) for t in texts]
            except Exception:
                pass
        return [self._heuristic_estimate(t) for t in texts]

    @staticmethod
    def _heuristic_estimate(text: str) -> int:
        """
        Heuristic token estimation when tiktoken is unavailable.
        English: ~4 chars per token
        CJK: ~1.5 chars per token (each character is roughly 1 token in modern encoders)
        Mixed: weighted average
        """
        if not text:
            return 0

        cjk_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - cjk_chars

        # CJK chars are roughly 1 token each in cl100k_base
        # English words are roughly 0.75 tokens per word, or 4 chars per token
        cjk_tokens = cjk_chars
        other_tokens = other_chars / 4.0

        return int(cjk_tokens + other_tokens)


# Global default estimator
_default_estimator: Optional[TokenEstimator] = None


def get_default_estimator() -> TokenEstimator:
    global _default_estimator
    if _default_estimator is None:
        _default_estimator = TokenEstimator()
    return _default_estimator


def estimate_tokens(text: str) -> int:
    return get_default_estimator().estimate(text)
