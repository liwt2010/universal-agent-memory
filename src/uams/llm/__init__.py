"""LLM client implementations for UAMS.

Public API:
    LLMClient              -- abstract base
    OpenAICompatibleClient -- OpenAI / MiniMax / ollama / vLLM (OpenAI-compatible endpoint)
    NullLLMClient          -- always raises; sentinel for tests / forced fallback
    CachedLLMClient        -- wraps any client with an in-process LRU cache
"""

from uams.llm.client import (
    CachedLLMClient,
    LLMClient,
    NullLLMClient,
    OpenAICompatibleClient,
)

__all__ = ["LLMClient", "OpenAICompatibleClient", "NullLLMClient", "CachedLLMClient"]