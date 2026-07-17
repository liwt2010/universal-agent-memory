"""Regression test for T05 (P1-1): ollama validator.

Pins that 'ollama' is a valid llm_provider value. Before this fix
the validator rejected it, blocking local-ollama deployments even
though the OpenAICompatibleClient transport is fully compatible
with ollama's /v1/chat/completions endpoint.
"""

from __future__ import annotations

import unittest

from uams.config import UAMSConfig


class TestOllamaValidator(unittest.TestCase):
    def test_ollama_provider_passes_validation(self) -> None:
        cfg = UAMSConfig(
            llm_enabled=True,
            llm_provider="ollama",
            llm_api_key="ollama",  # required even though unused
            llm_base_url="http://localhost:11434/v1",
            llm_model="llama3.1",
        )
        # validate() returns None on success, raises ValueError on failure
        try:
            result = cfg.validate()
        except ValueError as exc:
            self.fail(f"ollama config should validate, got: {exc}")
        self.assertIsNone(result)

    def test_openai_compatible_still_passes(self) -> None:
        cfg = UAMSConfig(
            llm_enabled=True,
            llm_provider="openai_compatible",
            llm_api_key="sk-test",
            llm_model="gpt-4o-mini",
        )
        try:
            result = cfg.validate()
        except ValueError as exc:
            self.fail(f"openai_compatible should still pass, got: {exc}")
        self.assertIsNone(result)

    def test_null_still_passes(self) -> None:
        cfg = UAMSConfig(llm_enabled=False, llm_provider="null")
        try:
            result = cfg.validate()
        except ValueError as exc:
            self.fail(f"null provider should still pass, got: {exc}")
        self.assertIsNone(result)

    def test_unknown_provider_still_rejected(self) -> None:
        cfg = UAMSConfig(
            llm_enabled=True,
            llm_provider="anthropic_native",  # not yet supported
            llm_api_key="sk-test",
        )
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("llm_provider", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()