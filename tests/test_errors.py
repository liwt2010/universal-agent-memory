"""Regression tests for the UAMS exception hierarchy (T04).

Pins:
- All error classes are reachable from package root
- Each subclass inherits from UAMSError
- Subclasses are distinct from each other (so `except ConfigError`
  does not also catch `StorageError`)
"""

from __future__ import annotations

import unittest


class TestErrorsReexport(unittest.TestCase):
    def test_imports_from_package_root(self) -> None:
        from uams import UAMSError, ConfigError, StorageError, CascadeError, LLMError

        # all five must be distinct classes
        classes = {UAMSError, ConfigError, StorageError, CascadeError, LLMError}
        self.assertEqual(len(classes), 5)

    def test_subclasses_inherit_from_uams_error(self) -> None:
        from uams import (
            UAMSError,
            ConfigError,
            StorageError,
            CascadeError,
            LLMError,
        )

        for cls in (ConfigError, StorageError, CascadeError, LLMError):
            self.assertTrue(
                issubclass(cls, UAMSError),
                msg=f"{cls.__name__} must inherit from UAMSError",
            )

    def test_can_catch_subclass_via_uams_error(self) -> None:
        from uams import UAMSError, ConfigError

        try:
            raise ConfigError("bad config")
        except UAMSError as exc:
            self.assertIsInstance(exc, ConfigError)

    def test_subclasses_are_siblings(self) -> None:
        """ConfigError must NOT also be a StorageError, etc."""
        from uams import ConfigError, StorageError, CascadeError, LLMError

        self.assertFalse(issubclass(ConfigError, StorageError))
        self.assertFalse(issubclass(StorageError, CascadeError))
        self.assertFalse(issubclass(CascadeError, LLMError))
        self.assertFalse(issubclass(LLMError, ConfigError))

    def test_subclasses_have_meaningful_str(self) -> None:
        """Subclass str(msg) must include the message (basic Exception behavior)."""
        from uams import ConfigError, StorageError, CascadeError, LLMError

        for cls in (ConfigError, StorageError, CascadeError, LLMError):
            err = cls("sample")
            self.assertIn("sample", str(err))


if __name__ == "__main__":
    unittest.main()