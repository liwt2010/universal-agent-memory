"""Regression test for T14 (P2-4): achat retries transient failures.

Pins the v0.6.0 retry semantics on OpenAICompatibleClient.achat:
- httpx.TimeoutException triggers retry (3 attempts total)
- httpx.ConnectError triggers retry
- 429 / 5xx HTTPStatusError trigger retry
- Other 4xx (e.g. 401, 400) DO NOT retry — bubble up immediately
- All attempts exhausted → re-raise the last exception
- Successful response on attempt 1 OR attempt 2 → returns content
  (no retry needed)
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from uams.llm.client import OpenAICompatibleClient


def _build_mock_client() -> OpenAICompatibleClient:
    """Construct an OpenAICompatibleClient without going through
    __init__ (which would import openai and require an api_key).
    """
    c = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    c._model = "m"
    c._base_url = "http://test"
    c._api_key = "k"
    c._timeout = 1.0
    c._max_retries = 0
    c._async_client = None
    return c


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError for the given status code."""
    request = httpx.Request("POST", "http://test/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _make_async_client_post_side_effect(
    side_effects: list[Any],
) -> AsyncMock:
    """Build an async mock whose .post(...) returns/raises in order."""
    mock_post = AsyncMock(side_effect=side_effects)
    return mock_post


class TestAchatRetry(unittest.TestCase):
    def test_succeeds_first_attempt(self) -> None:
        client = _build_mock_client()
        # Successful response with content
        good_resp = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello"}}]},
            request=httpx.Request("POST", "http://test/chat/completions"),
        )
        async_client = MagicMock()
        async_client.post = AsyncMock(return_value=good_resp)
        async_client.aclose = AsyncMock()
        client._async_client = async_client

        result = asyncio.run(client.achat([{"role": "user", "content": "hi"}]))
        self.assertEqual(result, "hello")
        # Called exactly once
        self.assertEqual(async_client.post.await_count, 1)

    def test_retries_on_timeout_then_succeeds(self) -> None:
        client = _build_mock_client()
        good_resp = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=httpx.Request("POST", "http://test/chat/completions"),
        )
        async_client = MagicMock()
        async_client.post = AsyncMock(
            side_effect=[httpx.ConnectTimeout("slow"), good_resp]
        )
        async_client.aclose = AsyncMock()
        client._async_client = async_client

        result = asyncio.run(client.achat([{"role": "user", "content": "hi"}]))
        self.assertEqual(result, "ok")
        self.assertEqual(async_client.post.await_count, 2)

    def test_retries_on_429(self) -> None:
        client = _build_mock_client()
        good_resp = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=httpx.Request("POST", "http://test/chat/completions"),
        )
        async_client = MagicMock()
        async_client.post = AsyncMock(
            side_effect=[_http_status_error(429), good_resp]
        )
        async_client.aclose = AsyncMock()
        client._async_client = async_client

        result = asyncio.run(client.achat([{"role": "user", "content": "hi"}]))
        self.assertEqual(result, "ok")
        self.assertEqual(async_client.post.await_count, 2)

    def test_retries_on_500(self) -> None:
        client = _build_mock_client()
        good_resp = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=httpx.Request("POST", "http://test/chat/completions"),
        )
        async_client = MagicMock()
        async_client.post = AsyncMock(
            side_effect=[_http_status_error(500), good_resp]
        )
        async_client.aclose = AsyncMock()
        client._async_client = async_client

        result = asyncio.run(client.achat([{"role": "user", "content": "hi"}]))
        self.assertEqual(result, "ok")
        self.assertEqual(async_client.post.await_count, 2)

    def test_no_retry_on_4xx_other_than_429(self) -> None:
        client = _build_mock_client()
        async_client = MagicMock()
        async_client.post = AsyncMock(
            side_effect=[_http_status_error(401)]
        )
        async_client.aclose = AsyncMock()
        client._async_client = async_client

        with self.assertRaises(httpx.HTTPStatusError) as ctx:
            asyncio.run(client.achat([{"role": "user", "content": "hi"}]))
        self.assertEqual(ctx.exception.response.status_code, 401)
        # Called exactly once — no retry
        self.assertEqual(async_client.post.await_count, 1)

    def test_all_attempts_exhausted_raises_last(self) -> None:
        client = _build_mock_client()
        async_client = MagicMock()
        async_client.post = AsyncMock(
            side_effect=[
                httpx.ConnectError("nope"),
                httpx.ConnectError("nope"),
                httpx.ConnectError("final"),
            ]
        )
        async_client.aclose = AsyncMock()
        client._async_client = async_client

        with self.assertRaises(httpx.ConnectError):
            asyncio.run(client.achat([{"role": "user", "content": "hi"}]))
        self.assertEqual(async_client.post.await_count, 3)


if __name__ == "__main__":
    unittest.main()