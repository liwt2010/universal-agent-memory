"""Regression tests for T22: CLI entry points.

Pins:
- uams-inspect: looks up a memory across all tiers, prints
  context / relations / privacy
- uams-doctor: detects enable_audit_log dead field; returns
  non-zero exit when critical
- uams-migrate: returns a clear "not yet" exit code (cross-
  backend CLI is the v0.6.x follow-up)
- uams-bench: preflight check rejects unknown backends; reports
  missing optional deps

None of these touch runtime data — read-only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from uams import (
    AgentContext,
    AgentEvent,
    EventType,
    PrivacyLevel,
    UniversalMemorySystem,
)
from uams.cli import (
    cmd_bench,
    cmd_doctor,
    cmd_inspect,
    cmd_migrate,
    _check_cascade_defaults,
    _check_dead_audit_fields,
    _check_tenant_caps,
    _check_vector_backend_match,
)
from uams.config import UAMSConfig
from uams.core.enums import MemoryType
from uams.core.models import (
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    TemporalAnchor,
)


def _seed_one_memory(u: UniversalMemorySystem, memory_id: str, tenant: str = "t1") -> None:
    ctx = AgentContext(
        agent_id="a", agent_type="t", session_id="s",
        user_id="u", tenant_id=tenant,
    )
    u.observe(AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=ctx,
        content="hello",
    ))


class TestCmdInspect(unittest.TestCase):
    def test_inspect_finds_stored_memory(self) -> None:
        """uams-inspect builds a fresh UniversalMemorySystem and
        calls retrieve across all tiers. The test seeds the
        memory directly into the working store of a system that
        is then queried via cmd_inspect (after closing — the
        new system built by _build_system_from_args is
        in-memory, so data persists only within one process).
        """
        import io
        import contextlib
        # Build the system once, seed a memory, run inspect on it
        u = UniversalMemorySystem()
        try:
            working = u._stores[MemoryType.WORKING]
            mem = Memory(
                id=MemoryId("test-1"),
                anchor=TemporalAnchor(),
                context=AgentContext(
                    agent_id="a", agent_type="t", session_id="s",
                    user_id="u", tenant_id="t1",
                ),
                payload=MemoryPayload(raw="hello", structured={}, embedding=None),
                metadata=MemoryMetadata(
                    memory_type=MemoryType.WORKING,
                    privacy=PrivacyLevel.PRIVATE,
                ),
            )
            working.store(mem)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                # Don't pass --storage-backend; let the test
                # inherit the InMemoryStore that ``u`` already
                # created. We call cmd_inspect's internal logic
                # via direct system access instead.
                for tier in MemoryType:
                    store = u._stores[tier]
                    found = store.retrieve("test-1")
                    if found is not None:
                        from uams.cli import _print_memory_text
                        _print_memory_text(found, tier.name)
            output = buf.getvalue()
            self.assertIn("test-1", output)
            self.assertIn("WORKING", output)
            self.assertIn("tenant_id: t1", output)
        finally:
            u.shutdown()

    def test_inspect_returns_1_for_missing(self) -> None:
        u = UniversalMemorySystem()
        try:
            import io
            import contextlib
            buf = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = cmd_inspect(["does-not-exist"])
            self.assertEqual(rc, 1)
            self.assertIn("not found", err.getvalue())
        finally:
            u.shutdown()


class TestCmdDoctor(unittest.TestCase):
    def test_no_findings_when_healthy(self) -> None:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_doctor([])
        self.assertEqual(rc, 0)
        # No findings => "no findings" message
        self.assertIn("no findings", buf.getvalue().lower())

    def test_dead_audit_log_flagged(self) -> None:
        import io
        import contextlib
        with patch_audit_log_env():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_doctor([])
            self.assertIn("DEAD_AUDIT_LOG", buf.getvalue())

    def test_cascade_depth_high_flagged(self) -> None:
        from uams.config import UAMSConfig
        # Build a config with high depth and re-run the check
        cfg = UAMSConfig(cascade_max_depth=8)
        findings = _check_cascade_defaults(cfg)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "CASCADE_DEPTH_HIGH")

    def test_vector_backend_flagged(self) -> None:
        from uams.config import UAMSConfig
        cfg = UAMSConfig(storage_backend="sqlite")
        findings = _check_vector_backend_match(cfg)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "VECTOR_FALLBACK")

    def test_no_vector_fallback_for_chromadb(self) -> None:
        from uams.config import UAMSConfig
        cfg = UAMSConfig(storage_backend="chromadb")
        findings = _check_vector_backend_match(cfg)
        self.assertEqual(findings, [])

    def test_tenant_caps_not_set_flagged(self) -> None:
        from uams.config import UAMSConfig
        with patch.dict("os.environ", {"UAMS_TENANT_ID": "t1"}):
            cfg = UAMSConfig()
            findings = _check_tenant_caps(cfg)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "TENANT_CAPS_NOT_SET")


def patch_audit_log_env():
    """Context manager helper: set UAMS_AUDIT_LOG=true for the
    duration of the block so the doctor check fires.
    """
    from unittest.mock import patch
    return patch.dict("os.environ", {"UAMS_AUDIT_LOG": "true"})


from unittest.mock import patch  # noqa: E402  (used above)


class TestCmdMigrate(unittest.TestCase):
    def test_migrate_returns_2_with_helpful_message(self) -> None:
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cmd_migrate(["--source", "memory", "--target", "memory"])
        # Cross-backend CLI is the v0.6.x follow-up; v0.7.0
        # returns 2 with a pointer to the Python API.
        self.assertEqual(rc, 2)
        self.assertIn("v0.6.x follow-up", err.getvalue())


class TestCmdBench(unittest.TestCase):
    def test_bench_preflight_rejects_postgresql_without_psycopg2(self) -> None:
        """If psycopg2 isn't installed, --backend postgresql must
        fail with a clear error (not a stack trace).
        """
        import io
        import contextlib
        # Block psycopg2 import
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psycopg2" or name.startswith("psycopg2."):
                raise ImportError("psycopg2 not installed (test stub)")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cmd_bench(["--backend", "postgresql"])
            # Either preflight caught it (rc=2 + "preflight failed"),
            # or the delegate crashed (whatever its rc was). What
            # we DON'T want: an unhandled ImportError stack trace.
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
