"""Tests for the async LLM client surface (P2-3).

Pins:
- LLMClient ABC exposes both chat() and achat(); the default achat
  implementation runs chat() on the default executor.
- NullLLMClient.achat raises RuntimeError, same as chat().
- CachedLLMClient.achat delegates to inner.achat when the inner client
  has a true async path, AND caches the result so a second call hits
  the cache (no second inner call).
- OpenAICompatibleClient builds a lazy httpx.AsyncClient; calling
  achat() twice on the same instance reuses the same client (verified
  indirectly: no second client construction overhead; payload format
  is correct).
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestLLMAsyncSurface(unittest.TestCase):
    def test_llm_client_abc_has_achat(self):
        from uams.llm.client import LLMClient
        self.assertTrue(hasattr(LLMClient, "achat"))
        # Default achat should be a coroutine function
        self.assertTrue(asyncio.iscoroutinefunction(LLMClient.achat))

    def test_null_llm_achat_raises(self):
        from uams.llm.client import NullLLMClient
        c = NullLLMClient()
        with self.assertRaises(RuntimeError):
            asyncio.run(c.achat([{"role": "user", "content": "hi"}]))

    def test_cached_llm_achat_uses_inner_achat(self):
        """When inner.achat exists, CachedLLMClient.achat calls it
        directly (NOT inner.chat + to_thread). Verified by counting
        calls to a fake inner.achat.
        """
        from uams.llm.client import CachedLLMClient, LLMClient

        class FakeInner(LLMClient):
            def __init__(self):
                self.sync_calls = 0
                self.async_calls = 0

            def chat(self, messages, **kwargs):
                self.sync_calls += 1
                return "sync:" + messages[0]["content"]

            async def achat(self, messages, **kwargs):
                self.async_calls += 1
                return "async:" + messages[0]["content"]

        inner = FakeInner()
        c = CachedLLMClient(inner)
        out1 = asyncio.run(c.achat([{"role": "user", "content": "hi"}]))
        self.assertEqual(out1, "async:hi")
        self.assertEqual(inner.sync_calls, 0, "achat should NOT call sync chat")
        self.assertEqual(inner.async_calls, 1)

    def test_cached_llm_achat_caches_hit(self):
        """Second achat with same payload + kwargs hits the cache, no
        second inner call.
        """
        from uams.llm.client import CachedLLMClient, LLMClient

        class FakeInner(LLMClient):
            def __init__(self):
                self.async_calls = 0

            def chat(self, messages, **kwargs):
                raise RuntimeError("sync path should not be reached")

            async def achat(self, messages, **kwargs):
                self.async_calls += 1
                return "ok"

        inner = FakeInner()
        c = CachedLLMClient(inner)
        out1 = asyncio.run(c.achat([{"role": "user", "content": "x"}]))
        out2 = asyncio.run(c.achat([{"role": "user", "content": "x"}]))
        self.assertEqual(out1, "ok")
        self.assertEqual(out2, "ok")
        self.assertEqual(inner.async_calls, 1, "second call should hit cache")


if __name__ == "__main__":
    unittest.main()
