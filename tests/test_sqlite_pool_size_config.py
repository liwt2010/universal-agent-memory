"""Tests for UAMSConfig.sqlite_pool_size wiring.

P2-CON-5: UAMSConfig.sqlite_pool_size was declared but never passed
to SQLiteStore. UniversalMemorySystem._init_stores_from_config()
constructed SQLiteStore without pool_size, so the default 8 was
always used regardless of env (UAMS_SQLITE_POOL_SIZE=16 → still 8).
The fix passes the config value through.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestSqlitePoolSizeConfig(unittest.TestCase):
    def test_default_pool_size_is_8(self):
        """UAMSConfig.sqlite_pool_size default matches SQLiteStore default."""
        from uams.config import UAMSConfig
        self.assertEqual(UAMSConfig().sqlite_pool_size, 8)

    def test_pool_size_passed_to_sqlite_store(self):
        """Custom pool_size from UAMSConfig reaches SQLiteStore.__init__."""
        from uams.config import UAMSConfig
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "poolsz.db")
            config = UAMSConfig(
                storage_backend="sqlite",
                sqlite_path=path,
                sqlite_pool_size=3,
            )
            ums = UniversalMemorySystem(config=config)
            try:
                # 3 tiers (episodic/semantic/procedural) each get a
                # SQLiteStore with pool_size=3.
                episodic = ums._stores[MemoryType.EPISODIC]
                semantic = ums._stores[MemoryType.SEMANTIC]
                procedural = ums._stores[MemoryType.PROCEDURAL]
                self.assertEqual(episodic._pool_size, 3)
                self.assertEqual(semantic._pool_size, 3)
                self.assertEqual(procedural._pool_size, 3)
            finally:
                for store in ums._stores.values():
                    if hasattr(store, "close"):
                        try:
                            store.close()
                        except Exception:
                            pass

    def test_pool_size_from_env(self):
        """UAMS_SQLITE_POOL_SIZE env var flows through to SQLiteStore."""
        from uams.config import UAMSConfig
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType
        import dataclasses

        old = os.environ.get("UAMS_SQLITE_POOL_SIZE")
        old_backend = os.environ.get("UAMS_STORAGE_BACKEND")
        old_path = os.environ.get("UAMS_SQLITE_PATH")
        os.environ["UAMS_SQLITE_POOL_SIZE"] = "16"
        os.environ["UAMS_STORAGE_BACKEND"] = "sqlite"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "envpoolsz.db")
                os.environ["UAMS_SQLITE_PATH"] = path
                config = UAMSConfig.from_env()
                # Verify env was actually read.
                self.assertEqual(config.sqlite_pool_size, 16)
                ums = UniversalMemorySystem(config=config)
                try:
                    self.assertEqual(
                        ums._stores[MemoryType.EPISODIC]._pool_size, 16,
                    )
                finally:
                    for store in ums._stores.values():
                        if hasattr(store, "close"):
                            try:
                                store.close()
                            except Exception:
                                pass
        finally:
            for var, prev in (
                ("UAMS_SQLITE_POOL_SIZE", old),
                ("UAMS_STORAGE_BACKEND", old_backend),
                ("UAMS_SQLITE_PATH", old_path),
            ):
                if prev is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = prev


if __name__ == "__main__":
    unittest.main()