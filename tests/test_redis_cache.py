"""Tests for RedisCacheBackend + CachedLLMClient/Provider integration.

Uses mock Redis client (unittest.mock) so no real Redis is required.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.llm.client import CachedLLMClient, LLMClient
from uams.embedding.client import CachedEmbeddingProvider
from uams.embedding.base import EmbeddingProvider
from uams.config import UAMSConfig


class FakeLLM(LLMClient):
    def __init__(self, response="ok"):
        self._response = response
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self._response


class FakeEmbed(EmbeddingProvider):
    def __init__(self, dim: int = 4):
        self._dim = dim
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        return [float(hash(text) % 100 + i) / 100 for i in range(self._dim)]

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


class TestRedisCacheBackendImport(unittest.TestCase):
    """Verify the import + class instantiation logic without real Redis."""

    def test_import_error_raises_helpful_message(self):
        """If redis isn't installed, raise ImportError with install hint."""
        from uams.cache.redis_backend import RedisCacheBackend
        with patch.dict("sys.modules", {"redis": None}):
            with self.assertRaises(ImportError) as ctx:
                # The lazy import inside __init__ will fail
                RedisCacheBackend(host="localhost", port=6379)
            self.assertIn("redis", str(ctx.exception).lower())


class TestRedisCacheBackendWithMock(unittest.TestCase):
    """Mock the redis.Redis class so we can exercise the backend without a server."""

    def _make_backend(self, ping_succeeds=True, get_returns=None, set_succeeds=True):
        mock_redis_module = MagicMock()
        mock_client = MagicMock()
        mock_redis_module.Redis.return_value = mock_client
        # PING: side_effect=Exception makes the mock actually raise, so the
        # backend's try/except kicks in. return_value=Exception would just
        # return the instance (no raise), which doesn't exercise the failure path.
        if ping_succeeds:
            mock_client.ping.return_value = True
        else:
            mock_client.ping.side_effect = Exception("redis down")
        # GET response
        if get_returns is None:
            mock_client.get.return_value = None
        else:
            mock_client.get.return_value = get_returns
        # SET response
        if set_succeeds:
            mock_client.set.return_value = True
        else:
            mock_client.set.side_effect = Exception("write fail")

        with patch.dict("sys.modules", {"redis": mock_redis_module}):
            from uams.cache.redis_backend import RedisCacheBackend
            backend = RedisCacheBackend(host="test-host", port=1234, key_prefix="test:")
        return backend, mock_client

    def test_construction_calls_ping(self):
        backend, mock_client = self._make_backend(ping_succeeds=True)
        mock_client.ping.assert_called_once()
        self.assertTrue(backend.is_connected())

    def test_construction_handles_ping_failure(self):
        backend, mock_client = self._make_backend(ping_succeeds=False)
        self.assertFalse(backend.is_connected())

    def test_get_returns_value_on_hit(self):
        backend, mock_client = self._make_backend(get_returns="cached-value")
        self.assertEqual(backend.get("key1"), "cached-value")
        mock_client.get.assert_called_with("test:key1")

    def test_get_returns_none_on_miss(self):
        backend, mock_client = self._make_backend(get_returns=None)
        self.assertIsNone(backend.get("missing"))

    def test_get_returns_none_on_redis_error(self):
        backend, mock_client = self._make_backend(ping_succeeds=True)
        mock_client.get.side_effect = Exception("redis down")
        self.assertIsNone(backend.get("key1"))

    def test_get_returns_none_when_disconnected(self):
        backend, _ = self._make_backend(ping_succeeds=False)
        # get() should short-circuit when not connected
        self.assertIsNone(backend.get("any-key"))

    def test_put_writes_value(self):
        backend, mock_client = self._make_backend()
        backend.put("k1", "v1")
        mock_client.set.assert_called_with("test:k1", "v1")

    def test_put_with_ttl_uses_ex(self):
        """When TTL is configured at construction, SET is called with ex=..."""
        # Re-build backend with ttl
        mock_redis_module = MagicMock()
        mock_client = MagicMock()
        mock_redis_module.Redis.return_value = mock_client
        mock_client.ping.return_value = True
        with patch.dict("sys.modules", {"redis": mock_redis_module}):
            from uams.cache.redis_backend import RedisCacheBackend
            backend = RedisCacheBackend(
                host="test-host", port=1234, key_prefix="test:",
                ttl_seconds=120.0,
            )
        backend.put("k1", "v1")
        # SET should have been called with the EX (seconds) argument
        mock_client.set.assert_called_with("test:k1", "v1", ex=120)

    def test_put_silent_on_redis_error(self):
        backend, mock_client = self._make_backend()
        mock_client.set.side_effect = Exception("write fail")
        # Should not raise
        backend.put("k1", "v1")

    def test_put_noop_when_disconnected(self):
        backend, mock_client = self._make_backend(ping_succeeds=False)
        backend.put("k1", "v1")
        # set() should not have been called
        mock_client.set.assert_not_called()


class TestCachedLLMClientWithExternalCache(unittest.TestCase):
    """CachedLLMClient uses external callables when provided, in-memory LRU otherwise."""

    def test_external_cache_hit_avoids_inner_call(self):
        calls = []

        def fake_inner(messages, **kwargs):
            calls.append("inner")
            return "from-inner"

        def fake_get(key):
            return "from-cache"

        def fake_put(key, value):
            pass

        inner = FakeLLM()
        # Use a wrapper that records calls (avoid relying on inner.calls since
        # we want to verify the cache short-circuits BEFORE inner.chat)
        inner.chat = fake_inner  # type: ignore
        cached = CachedLLMClient(
            inner,
            max_entries=10,
            cache_get=fake_get,
            cache_put=fake_put,
        )
        result = cached.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "from-cache")
        self.assertEqual(calls, [])  # inner.chat was never called

    def test_external_cache_miss_invokes_inner_then_stores(self):
        stored = []

        inner = FakeLLM(response="computed")
        cached = CachedLLMClient(
            inner,
            max_entries=10,
            cache_get=lambda k: None,
            cache_put=lambda k, v: stored.append((k, v)),
        )
        result = cached.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "computed")
        self.assertEqual(inner.calls, 1)
        self.assertEqual(len(stored), 1)
        # Key, value pair was stored
        self.assertEqual(stored[0][1], "computed")

    def test_external_cache_hits_return_consistently(self):
        """Same query -> same cache_hit regardless of inner state."""
        inner = FakeLLM(response="v1")
        counter = {"hits": 0}

        def fake_get(key):
            counter["hits"] += 1
            return "v1"  # always hit

        cached = CachedLLMClient(
            inner,
            max_entries=10,
            cache_get=fake_get,
            cache_put=lambda k, v: None,
        )
        # Call 3 times
        for _ in range(3):
            self.assertEqual(
                cached.chat([{"role": "user", "content": "x"}]),
                "v1",
            )
        # inner was never called (cache always hit)
        self.assertEqual(inner.calls, 0)
        self.assertEqual(counter["hits"], 3)

    def test_no_external_cache_uses_in_process_lru(self):
        """Backward compat: no cache_get/cache_put = in-process LRU."""
        inner = FakeLLM(response="v1")
        cached = CachedLLMClient(inner, max_entries=10)  # no external callables
        # First call -> miss, second -> hit
        cached.chat([{"role": "user", "content": "x"}])
        cached.chat([{"role": "user", "content": "x"}])
        self.assertEqual(inner.calls, 1)


class TestCachedEmbeddingProviderWithExternalCache(unittest.TestCase):
    """CachedEmbeddingProvider uses external callables for cross-process sharing."""

    def test_external_cache_serializes_vector(self):
        """List[float] is JSON-serialized through the external cache."""
        inner = FakeEmbed(dim=4)
        store = {}

        def fake_get(key):
            return store.get(key)

        def fake_put(key, value):
            store[key] = value

        cached = CachedEmbeddingProvider(
            inner,
            max_entries=10,
            cache_get=fake_get,
            cache_put=fake_put,
        )
        result = cached.embed("hello")
        self.assertEqual(len(result), 4)
        # Inner was called once
        self.assertEqual(inner.calls, 1)
        # The external store now contains a JSON-serialized version
        self.assertEqual(len(store), 1)
        # The stored value is a string (JSON array)
        for v in store.values():
            self.assertIsInstance(v, str)
            self.assertTrue(v.startswith("["))

    def test_external_cache_hit_returns_deserialized_vector(self):
        """Cache hit returns a properly-typed List[float], not a string."""
        inner = FakeEmbed(dim=3)
        cached = CachedEmbeddingProvider(
            inner,
            max_entries=10,
            cache_get=lambda k: "[0.1, 0.2, 0.3]",
            cache_put=lambda k, v: None,
        )
        result = cached.embed("anything")
        self.assertEqual(result, [0.1, 0.2, 0.3])
        # Inner never called
        self.assertEqual(inner.calls, 0)

    def test_external_cache_miss_falls_through_to_inner(self):
        inner = FakeEmbed(dim=2)
        stored = []
        cached = CachedEmbeddingProvider(
            inner,
            max_entries=10,
            cache_get=lambda k: None,
            cache_put=lambda k, v: stored.append((k, v)),
        )
        result = cached.embed("hello")
        self.assertEqual(len(result), 2)
        self.assertEqual(inner.calls, 1)
        self.assertEqual(len(stored), 1)

    def test_external_cache_corrupt_value_safely_returns_empty(self):
        """Corrupt JSON in cache shouldn't crash; returns empty vector."""
        inner = FakeEmbed(dim=2)
        cached = CachedEmbeddingProvider(
            inner,
            max_entries=10,
            cache_get=lambda k: "not-valid-json{",
            cache_put=lambda k, v: None,
        )
        # Should not raise; falls back to empty list
        result = cached.embed("hello")
        self.assertEqual(result, [])
        # Inner not called (deserialization returned [] but cache marked hit)
        # Actually, since get returned non-None, we treat it as a hit even if
        # the payload is corrupt -> graceful [] result without inner call
        self.assertEqual(inner.calls, 0)


class TestUAMSConfigCacheBackend(unittest.TestCase):
    """Config validation for the new cache_backend field."""

    def test_default_backend_is_memory(self):
        cfg = UAMSConfig()
        self.assertEqual(cfg.cache_backend, "memory")

    def test_invalid_backend_rejected(self):
        with self.assertRaises(ValueError):
            UAMSConfig(cache_backend="memcached").validate()

    def test_redis_backend_port_bounds(self):
        with self.assertRaises(ValueError):
            UAMSConfig(
                cache_backend="redis",
                redis_cache_port=70000,
            ).validate()

    def test_redis_backend_ttl_must_be_positive(self):
        with self.assertRaises(ValueError):
            UAMSConfig(
                cache_backend="redis",
                redis_cache_ttl_seconds=-1.0,
            ).validate()

    def test_redis_backend_valid_config(self):
        cfg = UAMSConfig(
            cache_backend="redis",
            redis_cache_host="redis.prod",
            redis_cache_port=6380,
            redis_cache_db=2,
            redis_cache_ttl_seconds=3600.0,
        )
        cfg.validate()  # should not raise


if __name__ == "__main__":
    unittest.main()