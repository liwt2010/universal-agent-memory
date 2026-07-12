"""Tests for AsyncUniversalMemorySystem public API.

P2-DX-2: AsyncUniversalMemorySystem.forget() was typed -> bool but
since the cascade rewrite the sync version returns CascadeReport.
The async wrapper also dropped the cascade/max_depth/in_edge_mode
kwargs the sync version gained. This regression test pins both the
return type and the kwargs.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestAsyncForgetSignature(unittest.TestCase):
    def _make(self):
        from uams.async_system import AsyncUniversalMemorySystem
        from uams import UniversalMemorySystem
        return AsyncUniversalMemorySystem(ums=UniversalMemorySystem())

    def test_async_forget_returns_cascade_report(self):
        from uams.pipeline.cascade import CascadeReport, CascadeStrategy
        aus = self._make()
        report = asyncio.run(aus.forget("mem-1"))
        # Must be a CascadeReport, NOT a bool. This guards against
        # someone "fixing" the type hint back to bool.
        self.assertIsInstance(report, CascadeReport)
        self.assertEqual(report.target_id, "mem-1")
        self.assertEqual(report.strategy, CascadeStrategy.BIDIRECTIONAL)

    def test_async_forget_accepts_isolated_strategy(self):
        from uams.pipeline.cascade import CascadeReport, CascadeStrategy
        aus = self._make()
        report = asyncio.run(aus.forget(
            "mem-1",
            cascade=CascadeStrategy.ISOLATED,
        ))
        self.assertIsInstance(report, CascadeReport)
        self.assertEqual(report.strategy, CascadeStrategy.ISOLATED)

    def test_async_forget_accepts_max_depth_kwarg(self):
        """Forward max_depth to the sync forget()."""
        from uams.pipeline.cascade import CascadeReport
        aus = self._make()
        # max_depth=None means "use config default" — must not raise.
        report = asyncio.run(aus.forget("mem-1", max_depth=2))
        self.assertIsInstance(report, CascadeReport)

    def test_async_forget_accepts_in_edge_mode_kwarg(self):
        """Forward in_edge_mode to the sync forget()."""
        from uams.pipeline.cascade import CascadeReport
        aus = self._make()
        report = asyncio.run(aus.forget("mem-1", in_edge_mode="scan"))
        self.assertIsInstance(report, CascadeReport)


if __name__ == "__main__":
    unittest.main()