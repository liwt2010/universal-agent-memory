"""LLM client abstraction with OpenAI-compatible implementation.

The ``openai`` package is imported lazily inside ``OpenAICompatibleClient``
so that the rest of ``uams`` does not require it. Install with
``pip install 'universal-agent-memory[llm]'`` to enable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable
import hashlib
import logging
import threading
import asyncio
import time

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract LLM client. Implementations live in this module.

    Both ``chat`` (sync) and ``achat`` (async) are part of the interface.
    Implementations that cannot provide a true async path (because
    the underlying SDK is blocking) should raise
    ``NotImplementedError`` from ``achat`` so callers fall back to
    ``asyncio.to_thread(client.chat, ...)`` rather than silently
    blocking the event loop.
    """

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> str:
        """Send a chat completion request and return the assistant message content."""

    async def achat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> str:
        """Async chat completion. Default impl runs ``chat`` on the default
        executor. Subclasses that have a true async transport should
        override to avoid the executor hop.
        """
        return await asyncio.to_thread(
            self.chat, messages,
            max_tokens=max_tokens, temperature=temperature, timeout=timeout,
        )

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
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = float(timeout)

    def chat(self, messages, *, max_tokens=1024, temperature=0.0, timeout=30.0):
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        return (resp.choices[0].message.content or "")

    async def achat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> str:
        """True async chat completion via ``httpx.AsyncClient``.

        Bypasses the openai SDK's blocking transport. The transport is
        lazy: a single ``httpx.AsyncClient`` is constructed on first
        call and reused for the lifetime of this LLM client instance.
        """
        import httpx  # local import — httpx is a soft dep for async paths

        body = {
            "model": self._model,
            "messages": list(messages),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        # Lazy transport: build once, reuse. LLM client lifetime is
        # typically the whole program, so a per-instance client is fine.
        client = getattr(self, "_async_client", None)
        if client is None:
            client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=float(timeout) if timeout else self._timeout,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            self._async_client = client
        try:
            resp = await client.post("/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("AsyncLLM HTTP call failed: %s", exc)
            raise
        # OpenAI-shaped response: choices[0].message.content
        choices = data.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content") or ""

    async def aclose(self) -> None:
        """Close the lazy httpx.AsyncClient. Call before shutdown to
        avoid 'Unclosed client' warnings.
        """
        client = getattr(self, "_async_client", None)
        if client is not None:
            await client.aclose()
            self._async_client = None


class NullLLMClient(LLMClient):
    """Always raises. Sentinel for tests and forced fallback."""

    def chat(self, messages, **kwargs):  # noqa: ARG002
        raise RuntimeError("NullLLMClient cannot serve requests")

    async def achat(self, messages, **kwargs):  # noqa: ARG002
        raise RuntimeError("NullLLMClient cannot serve requests")


class CachedLLMClient(LLMClient):
    """Wraps another client with a (messages, kwargs) -> response cache.

    Bounded LRU by default. To share cache across processes, pass
    ``cache_get`` / ``cache_put`` callables (e.g. from
    ``RedisCacheBackend``). When both are provided, the in-process LRU
    is bypassed entirely and every read/write goes through the external
    backend — which is expected to be shared across workers.

    Optional ``ttl_seconds``: if set, cached entries carry an expiry
    timestamp and stale entries are treated as cache misses on read.
    Without TTL the behavior is the original "cache forever" — enabled
    only by explicit opt-in so existing deployments keep working.
    ``ttl_seconds <= 0`` is treated as ``None`` (defensive).

    Thread-safe via RLock when using the in-process backend.
    """

    def __init__(
        self,
        inner: LLMClient,
        max_entries: int = 1000,
        cache_get: Callable[[str], str | None] = None,
        cache_put: Callable[[str, str], None] | None = None,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._inner = inner
        self._external = cache_get is not None and cache_put is not None
        self._ttl = float(ttl_seconds) if (ttl_seconds and ttl_seconds > 0) else None
        self._clock = clock
        if self._external:
            self._cache_get = cache_get
            self._cache_put = cache_put
        else:
            self._max = max(1, int(max_entries))
            self._cache: dict[str, str] = {}
            self._lock = threading.RLock()

    def _encode(self, value: str, now: float) -> str:
        # Without TTL we store the value verbatim — backward-compatible
        # with anything already in an external backend.
        if self._ttl is None:
            return value
        return f"{value}|{now + self._ttl:.6f}"

    def _decode(self, raw: str | None, now: float) -> str | None:
        if raw is None:
            return None
        if self._ttl is None:
            return raw
        sep = raw.rfind("|")
        if sep < 0:
            # No envelope and TTL is on — treat as stale rather than trust
            # an unknown format. Returning None signals cache miss.
            return None
        body, exp_str = raw[:sep], raw[sep + 1 :]
        try:
            exp = float(exp_str)
        except ValueError:
            return None
        if now >= exp:
            return None
        return body

    def chat(self, messages, **kwargs):
        key_payload = repr(messages) + "|" + repr(sorted(kwargs.items()))
        key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        now = self._clock()
        # Cache lookup
        if self._external:
            cached = self._decode(self._cache_get(key), now)
        else:
            with self._lock:
                raw = self._cache.get(key)
            cached = self._decode(raw, now)
        if cached is not None:
            return cached
        result = self._inner.chat(messages, **kwargs)
        # Cache write
        encoded = self._encode(result, now)
        if self._external:
            self._cache_put(key, encoded)
        else:
            with self._lock:
                if len(self._cache) >= self._max:
                    # Drop oldest (insertion-ordered dict semantics in py3.7+)
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = encoded
        return result

    async def achat(self, messages, **kwargs):
        """Async variant. Mirrors chat() but delegates to inner.achat
        when the inner client has a true async path, so the executor
        hop is avoided. Cache lookup/store uses the same in-process
        dict or external backend as the sync path.
        """
        key_payload = repr(messages) + "|" + repr(sorted(kwargs.items()))
        key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
        now = self._clock()
        # Cache lookup
        if self._external:
            cached = self._decode(self._cache_get(key), now)
        else:
            with self._lock:
                raw = self._cache.get(key)
            cached = self._decode(raw, now)
        if cached is not None:
            return cached
        # Inner call — use true async path if available, else hop the
        # executor (the default LLMClient.achat does this for us).
        if hasattr(self._inner, "achat"):
            result = await self._inner.achat(messages, **kwargs)
        else:
            result = await asyncio.to_thread(self._inner.chat, messages, **kwargs)
        encoded = self._encode(result, now)
        if self._external:
            self._cache_put(key, encoded)
        else:
            with self._lock:
                if len(self._cache) >= self._max:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = encoded
        return result

    def is_available(self) -> bool:
        return self._inner.is_available()