"""LLM client abstraction with OpenAI-compatible implementation.

The ``openai`` package is imported lazily inside ``OpenAICompatibleClient``
so that the rest of ``uams`` does not require it. Install with
``pip install 'universal-agent-memory[llm]'`` to enable.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional
import hashlib
import logging
import threading

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract LLM client. Implementations live in this module."""

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> str:
        """Send a chat completion request and return the assistant message content."""

    def is_available(self) -> bool:
        """Return True if this client can serve requests."""
        return True


class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible client.

    Works with OpenAI, MiniMax, ollama (in OpenAI-compat mode), vLLM, and any
    other provider that exposes an OpenAI-shaped ``/chat/completions`` endpoint.

    Set ``base_url`` to the provider's endpoint, e.g.:

    - OpenAI:      ``https://api.openai.com/v1``
    - MiniMax:     ``https://api.minimaxi.com/v1``
    - ollama:      ``http://localhost:11434/v1``
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "openai package required. Install: pip install 'universal-agent-memory[llm]'"
            ) from exc
        if not api_key:
            raise ValueError("api_key is required for OpenAICompatibleClient")
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._model = model

    def chat(self, messages, *, max_tokens=1024, temperature=0.0, timeout=30.0):
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        return (resp.choices[0].message.content or "")


class NullLLMClient(LLMClient):
    """Always raises. Sentinel for tests and forced fallback."""

    def chat(self, messages, **kwargs):  # noqa: ARG002
        raise RuntimeError("NullLLMClient cannot serve requests")


class CachedLLMClient(LLMClient):
    """Wraps another client with a (messages, kwargs) -> response cache.

    Bounded LRU by default. To share cache across processes, pass
    ``cache_get`` / ``cache_put`` callables (e.g. from
    ``RedisCacheBackend``). When both are provided, the in-process LRU
    is bypassed entirely and every read/write goes through the external
    backend — which is expected to be shared across workers.

    Thread-safe via RLock when using the in-process backend.
    """

    def __init__(
        self,
        inner: LLMClient,
        max_entries: int = 1000,
        cache_get: Optional[Callable[[str], Optional[str]]] = None,
        cache_put: Optional[Callable[[str, str], None]] = None,
    ):
        self._inner = inner
        self._external = cache_get is not None and cache_put is not None
        if self._external:
            self._cache_get = cache_get
            self._cache_put = cache_put
        else:
            self._max = max(1, int(max_entries))
            self._cache: Dict[str, str] = {}
            self._lock = threading.RLock()

    def chat(self, messages, **kwargs):
        key_payload = repr(messages) + "|" + repr(sorted(kwargs.items()))
        key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        # Cache lookup
        if self._external:
            cached = self._cache_get(key)
        else:
            with self._lock:
                cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._inner.chat(messages, **kwargs)
        # Cache write
        if self._external:
            self._cache_put(key, result)
        else:
            with self._lock:
                if len(self._cache) >= self._max:
                    # Drop oldest (insertion-ordered dict semantics in py3.7+)
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = result
        return result

    def is_available(self) -> bool:
        return self._inner.is_available()