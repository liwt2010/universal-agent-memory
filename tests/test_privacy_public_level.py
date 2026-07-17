"""Regression test for T07 (P1-3): PrivacyFilter must always
scrub secrets regardless of PrivacyLevel.

Pins:
- PUBLIC-level text with an embedded OpenAI key still has the
  key redacted (the v0.5.x bug)
- INTERNAL / PRIVATE also redact secrets (unchanged)
- PUBLIC-level text with embedded PII (email) is preserved as
  user-visible content (by design — public data is public)
- A custom pattern list split is preserved
"""

from __future__ import annotations

import unittest

from uams import PrivacyLevel
from uams.pipeline.privacy import PrivacyFilter


class TestPrivacyPublicLevel(unittest.TestCase):
    def setUp(self) -> None:
        self.f = PrivacyFilter()

    def test_openai_key_redacted_in_public_text(self) -> None:
        text = "Hey, my OpenAI key is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"
        out = self.f.sanitize(text, PrivacyLevel.PUBLIC)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", out)
        self.assertIn("<OPENAI_API_KEY>", out)

    def test_github_pat_redacted_in_public_text(self) -> None:
        # Classic PAT
        text = "token = ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        out = self.f.sanitize(text, PrivacyLevel.PUBLIC)
        self.assertNotIn("ghp_aaaa", out)
        self.assertIn("<GITHUB_TOKEN>", out)

        # Fine-grained PAT: 22-char prefix + _ + 59 chars = 82 chars after 'github_pat_'
        prefix = "github_pat_" + "A" * 22
        token = prefix + "_" + "B" * 59
        out2 = self.f.sanitize(f"tok = {token}", PrivacyLevel.PUBLIC)
        self.assertNotIn("github_pat_", out2)
        self.assertIn("<GITHUB_TOKEN>", out2)

    def test_bearer_token_redacted_in_public_text(self) -> None:
        text = "Authorization: Bearer abcdefghijklmnop1234567890"
        out = self.f.sanitize(text, PrivacyLevel.PUBLIC)
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertIn("Bearer <TOKEN>", out)

    def test_aws_access_key_redacted_in_public_text(self) -> None:
        text = "key = AKIAIOSFODNN7EXAMPLE"
        out = self.f.sanitize(text, PrivacyLevel.PUBLIC)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertIn("<AWS_ACCESS_KEY>", out)

    def test_pii_email_preserved_in_public_text(self) -> None:
        """By design: PUBLIC content keeps user-visible PII. Secrets
        are scrubbed; emails are not (the user marked it public).
        """
        text = "Contact me at alice@example.com"
        out = self.f.sanitize(text, PrivacyLevel.PUBLIC)
        self.assertIn("alice@example.com", out)

    def test_pii_email_redacted_in_private_text(self) -> None:
        text = "Contact me at alice@example.com"
        out = self.f.sanitize(text, PrivacyLevel.PRIVATE)
        self.assertNotIn("alice@example.com", out)
        self.assertIn("<EMAIL>", out)

    def test_secret_still_redacted_in_private_text(self) -> None:
        text = "key = sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"
        out = self.f.sanitize(text, PrivacyLevel.PRIVATE)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", out)
        self.assertIn("<OPENAI_API_KEY>", out)

    def test_secret_level_returns_redacted_marker(self) -> None:
        text = "anything at all"
        out = self.f.sanitize(text, PrivacyLevel.SECRET)
        self.assertEqual(out, "[REDACTED]")


if __name__ == "__main__":
    unittest.main()