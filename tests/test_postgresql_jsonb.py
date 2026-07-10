"""Tests for PostgreSQL JSONB deserialization compatibility.

These tests do NOT require a real PostgreSQL server. They cover the helper
that handles both cases:
  - JSON string (old psycopg2 <2.9 or mock returns raw str)
  - Already-decoded dict/list (psycopg2 2.9+ JSONB auto-deserialize)
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestCoerceJson(unittest.TestCase):

    def test_none_passes_through(self):
        from uams.storage.postgresql import PostgreSQLStore
        self.assertIsNone(PostgreSQLStore._coerce_json(None))

    def test_dict_passes_through(self):
        from uams.storage.postgresql import PostgreSQLStore
        d = {"a": 1, "b": [1, 2]}
        self.assertEqual(PostgreSQLStore._coerce_json(d), d)
        # identity — same dict object, not a copy
        self.assertIs(PostgreSQLStore._coerce_json(d), d)

    def test_list_passes_through(self):
        from uams.storage.postgresql import PostgreSQLStore
        lst = [1, 2, "three"]
        self.assertIs(PostgreSQLStore._coerce_json(lst), lst)

    def test_json_string_round_trips(self):
        from uams.storage.postgresql import PostgreSQLStore
        s = json.dumps({"a": 1, "b": [1, 2]}, sort_keys=True)
        out = PostgreSQLStore._coerce_json(s)
        self.assertEqual(out, {"a": 1, "b": [1, 2]})

    def test_bytes_decoded_to_string_then_loaded(self):
        from uams.storage.postgresql import PostgreSQLStore
        b = json.dumps({"x": True}).encode("utf-8")
        self.assertEqual(PostgreSQLStore._coerce_json(b), {"x": True})


class TestImportSurface(unittest.TestCase):
    """Static checks: forward refs in system.py and logger in retrieval.py used to fail flake8 F821."""

    def test_system_imports_LLMClient_at_top(self):
        import uams.system as s
        self.assertIn("LLMClient", dir(s))

    def test_retrieval_has_logger(self):
        import uams.pipeline.retrieval as r
        self.assertTrue(hasattr(r, "logger"))


if __name__ == "__main__":
    unittest.main()
