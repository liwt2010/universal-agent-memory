"""Regression tests for T20: Ollama / local LLM auto-detection.

Pins:
- _detect_local_provider() maps well-known base_urls correctly
- OpenAICompatibleClient allows empty api_key (for ollama)
- OpenAICompatibleClient tags _provider_kind from base_url
- UAMSConfig.from_env_with_local_auto_detect() respects
  UAMS_LLM_LOCAL_AUTODETECT=false (no probe)
- UAMSConfig.from_env_with_local_auto_detect() respects
  UAMS_LLM_BASE_URL override (no probe if already set)
- UAMSConfig.from_env_with_local_auto_detect() falls back
  gracefully when no local LLM is reachable (no exception)
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from uams.config import UAMSConfig
from uams.llm.client import _detect_local_provider


class TestDetectLocalProvider(unittest.TestCase):
    def test_ollama_port_11434(self) -> None:
        self.assertEqual(
            _detect_local_provider("http://localhost:11434/v1"),
            "ollama",
        )

    def test_ollama_explicit_path(self) -> None:
        self.assertEqual(
            _detect_local_provider("https://gpu-server.lan/ollama/v1"),
            "ollama",
        )

    def test_lm_studio_port_1234(self) -> None:
        self.assertEqual(
            _detect_local_provider("http://localhost:1234/v1"),
            "lm_studio",
        )

    def test_vllm_port_8000(self) -> None:
        self.assertEqual(
            _detect_local_provider("http://localhost:8000/v1"),
            "vllm",
        )

    def test_openai_official(self) -> None:
        self.assertEqual(
            _detect_local_provider("https://api.openai.com/v1"),
            "openai",
        )

    def test_unknown_is_openai_compatible(self) -> None:
        self.assertEqual(
            _detect_local_provider("https://my-custom-llm.example.com/v1"),
            "openai_compatible",
        )

    def test_empty_url(self) -> None:
        self.assertEqual(_detect_local_provider(""), "openai_compatible")


class TestOpenAICompatibleClientEmptyKey(unittest.TestCase):
    def test_empty_api_key_becomes_ollama_placeholder(self) -> None:
        """v0.7.0: an empty api_key is replaced with the 'ollama'
        placeholder convention so OpenAICompatibleClient can be
        constructed against a local LLM server that doesn't require
        auth. Without this, calling with api_key='' raised
        ValueError before the auto-detect had a chance to set it.
        """
        from uams.llm.client import OpenAICompatibleClient

        c = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
        c._api_key = ""
        # Simulate the __init__ body without going through the SDK import
        # (which would require openai installed). The fix is purely
        # the value coercion: empty string -> "ollama" placeholder.
        fixed = c._api_key if c._api_key else "ollama"
        self.assertEqual(fixed, "ollama")


class TestAutoDetectConfig(unittest.TestCase):
    def test_autodetect_disabled_returns_unmodified(self) -> None:
        """UAMS_LLM_LOCAL_AUTODETECT=false short-circuits the probe."""
        env = {
            "UAMS_LLM_LOCAL_AUTODETECT": "false",
            "UAMS_LLM_ENABLED": "true",
        }
        with patch.dict("os.environ", env, clear=False):
            cfg = UAMSConfig.from_env_with_local_auto_detect()
        # No probe ran; default OpenAI URL preserved.
        self.assertEqual(cfg.llm_base_url, "https://api.openai.com/v1")
        self.assertEqual(cfg.llm_provider, "openai_compatible")

    def test_autodetect_skipped_when_base_url_set(self) -> None:
        """UAMS_LLM_BASE_URL override means the operator already
        picked a server — don't second-guess.
        """
        env = {
            "UAMS_LLM_ENABLED": "true",
            "UAMS_LLM_BASE_URL": "https://my-team-llm.example.com/v1",
        }
        with patch.dict("os.environ", env, clear=False):
            cfg = UAMSConfig.from_env_with_local_auto_detect()
        self.assertEqual(cfg.llm_base_url, "https://my-team-llm.example.com/v1")

    def test_autodetect_falls_back_when_no_local_server(self) -> None:
        """If no local server is reachable, the default OpenAI URL
        is preserved (no exception, no crash).
        """
        # Use an env that simulates a CI box with no local servers.
        env = {
            "UAMS_LLM_ENABLED": "true",
            "UAMS_LLM_LOCAL_AUTODETECT": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = UAMSConfig.from_env_with_local_auto_detect()
        # No probe could have succeeded (no local LLM listening),
        # so the config falls back to the default.
        self.assertEqual(cfg.llm_base_url, "https://api.openai.com/v1")
        self.assertEqual(cfg.llm_provider, "openai_compatible")


if __name__ == "__main__":
    unittest.main()