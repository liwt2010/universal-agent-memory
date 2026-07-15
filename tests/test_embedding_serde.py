"""Tests for utils.embedding_serde — JSON-first, pickle-fallback."""

import json
import os
import pickle
import sys
import unittest

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.utils.embedding_serde import (
    serialize_embedding,
    deserialize_embedding,
)


class TestSerializeEmbedding(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(serialize_embedding(None))

    def test_list_serializes_to_json_bytes(self):
        vec = [0.1, 0.2, 0.3, -0.5, 1.0]
        result = serialize_embedding(vec)
        self.assertIsInstance(result, bytes)
        # Must be valid JSON
        decoded = json.loads(result.decode("utf-8"))
        self.assertEqual(decoded, vec)

    def test_empty_list(self):
        result = serialize_embedding([])
        self.assertEqual(json.loads(result.decode("utf-8")), [])


class TestDeserializeEmbedding(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(deserialize_embedding(None))

    def test_empty_bytes_returns_none(self):
        self.assertIsNone(deserialize_embedding(b""))

    def test_json_roundtrip(self):
        vec = [0.1, 0.2, 0.3]
        blob = serialize_embedding(vec)
        self.assertEqual(deserialize_embedding(blob), vec)

    def test_legacy_pickle_blob_rejected_as_rce_protection(self):
        """Pickle blobs are now REFUSED, not deserialized.

        Previously the code fell back to pickle.loads when JSON decode
        failed, which was a remote-code-execution vector if an attacker
        could write to the backing store. The fix (v0.4.1 hardening)
        rejects all pickle blobs and logs an ERROR; operators must run
        the migration script in the embedding_serde module docstring
        before deploying this version.

        This test pins the new fail-secure behaviour so the pickle
        fallback cannot be silently re-introduced.
        """
        legacy_blob = pickle.dumps([0.5, -0.5, 1.0])
        # Sanity: legacy blob does start with the pickle marker.
        self.assertEqual(legacy_blob[:1], b"\x80")
        # The new behaviour: pickle blobs are refused, not executed.
        result = deserialize_embedding(legacy_blob)
        self.assertIsNone(
            result,
            "deserialize_embedding must NOT execute pickle.loads on "
            "an attacker-controllable blob (RCE prevention).",
        )

    def test_corrupt_blob_returns_none(self):
        """A blob that is neither valid JSON nor a pickle marker returns None."""
        result = deserialize_embedding(b"this is not json or pickle")
        # json.loads fails (JSONDecodeError); pickle.loads fails (UnpicklingError)
        # Should log and return None
        self.assertIsNone(result)

    def test_non_list_json_returns_none(self):
        """JSON decodes to something that is not a list -> None."""
        blob = json.dumps({"not": "a list"}).encode("utf-8")
        result = deserialize_embedding(blob)
        # Coerce path: dict is not list, returns None
        self.assertIsNone(result)

    def test_list_with_non_float_coerced(self):
        """JSON int values are coerced to float for type stability."""
        blob = json.dumps([1, 2, 3]).encode("utf-8")
        result = deserialize_embedding(blob)
        self.assertEqual(result, [1.0, 2.0, 3.0])

    def test_bytearray_input_handled(self):
        """bytearray (e.g. from sqlite3.Binary) should be accepted."""
        vec = [0.7, 0.8]
        blob = bytearray(json.dumps(vec).encode("utf-8"))
        result = deserialize_embedding(blob)
        self.assertEqual(result, vec)

    def test_memoryview_input_handled(self):
        """memoryview (e.g. from psycopg2 binary column) must be coerced
        to bytes before json.loads — psycopg2 returns memoryview, not bytes,
        and json.JSONDecoder rejects memoryview directly."""
        vec = [0.9, -0.1, 0.42]
        blob = memoryview(json.dumps(vec).encode("utf-8"))
        # Sanity: json.JSONDecoder really does reject memoryview
        import json as _json
        with self.assertRaises(TypeError):
            _json.loads(blob)
        # But our helper must handle it
        result = deserialize_embedding(blob)
        self.assertEqual(result, vec)


if __name__ == "__main__":
    unittest.main()
