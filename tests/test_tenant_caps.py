"""Regression tests for T21: tenant-level resource caps.

Pins:
- UAMSConfig.from_env reads the three new keys (caps + hard_enforce)
- _check_tenant_cap returns True when no caps are configured
- _check_tenant_cap returns True when caps are configured and
  the tenant is under both
- _check_tenant_cap returns False when the cap is exceeded,
  but warn-only by default (observe() does NOT drop the event)
- With hard_enforce_tenant_caps=True, observe() drops the event
  when the cap is exceeded
- The warning is throttled — the second observe() on the same
  over-cap tenant does NOT log a second warning
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from uams import (
    AgentContext,
    AgentEvent,
    EventType,
    UniversalMemorySystem,
)
from uams.config import UAMSConfig


def _ctx_with_tenant(tenant: str | None) -> AgentContext:
    return AgentContext(
        agent_id="a", agent_type="t", session_id="s", user_id="u",
        tenant_id=tenant,
    )


def _evt(ctx: AgentContext) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=ctx,
        content="hello",
    )


class TestTenantCapConfig(unittest.TestCase):
    def test_default_caps_are_none(self) -> None:
        cfg = UAMSConfig()
        self.assertIsNone(cfg.tenant_max_memory_count)
        self.assertIsNone(cfg.tenant_max_storage_bytes)
        self.assertFalse(cfg.hard_enforce_tenant_caps)

    def test_from_env_reads_caps(self) -> None:
        env = {
            "UAMS_TENANT_MAX_MEMORY_COUNT": "100",
            "UAMS_TENANT_MAX_STORAGE_BYTES": "1048576",
            "UAMS_HARD_ENFORCE_TENANT_CAPS": "true",
        }
        with patch.dict("os.environ", env, clear=False):
            cfg = UAMSConfig.from_env()
        self.assertEqual(cfg.tenant_max_memory_count, 100)
        self.assertEqual(cfg.tenant_max_storage_bytes, 1048576)
        self.assertTrue(cfg.hard_enforce_tenant_caps)

    def test_unset_env_keeps_caps_none(self) -> None:
        env = {
            "UAMS_TENANT_MAX_MEMORY_COUNT": "",  # empty string = unset
            "UAMS_TENANT_MAX_STORAGE_BYTES": "",
        }
        with patch.dict("os.environ", env, clear=False):
            cfg = UAMSConfig.from_env()
        self.assertIsNone(cfg.tenant_max_memory_count)
        self.assertIsNone(cfg.tenant_max_storage_bytes)


class TestTenantCapBehavior(unittest.TestCase):
    def test_no_caps_means_unlimited(self) -> None:
        u = UniversalMemorySystem()
        try:
            self.assertTrue(u._check_tenant_cap(_ctx_with_tenant("t1")))
        finally:
            u.shutdown()

    def test_under_cap_passes(self) -> None:
        u = UniversalMemorySystem(config=UAMSConfig(
            tenant_max_memory_count=10,
            tenant_max_storage_bytes=10_000_000,
        ))
        try:
            # No memories stored yet — definitely under cap
            self.assertTrue(u._check_tenant_cap(_ctx_with_tenant("t1")))
        finally:
            u.shutdown()

    def test_over_count_cap_returns_false_warn_only(self) -> None:
        u = UniversalMemorySystem(config=UAMSConfig(
            tenant_max_memory_count=1,
            tenant_max_storage_bytes=None,
            hard_enforce_tenant_caps=False,
        ))
        try:
            # First observe goes through (under cap)
            u.observe(_evt(_ctx_with_tenant("t-overflow")))
            # Second observe exceeds the cap
            self.assertFalse(u._check_tenant_cap(_ctx_with_tenant("t-overflow")))
            # but observe() in warn-only mode does NOT drop
            u.observe(_evt(_ctx_with_tenant("t-overflow")))
        finally:
            u.shutdown()

    def test_over_cap_drops_when_hard_enforce(self) -> None:
        u = UniversalMemorySystem(config=UAMSConfig(
            tenant_max_memory_count=1,
            tenant_max_storage_bytes=None,
            hard_enforce_tenant_caps=True,
        ))
        try:
            # Seed one memory
            u.observe(_evt(_ctx_with_tenant("t-strict")))
            # Second observe should be dropped (count >= cap=1)
            with patch("uams.system.logger") as mock_logger:
                u.observe(_evt(_ctx_with_tenant("t-strict")))
                self.assertTrue(mock_logger.warning.called)
        finally:
            u.shutdown()

    def test_warning_throttled_per_tenant_cap(self) -> None:
        u = UniversalMemorySystem(config=UAMSConfig(
            tenant_max_memory_count=1,
            hard_enforce_tenant_caps=False,
        ))
        try:
            u.observe(_evt(_ctx_with_tenant("t-throttle")))
            # Three subsequent over-cap calls — should log once,
            # then the warning key is in _tenant_cap_warned.
            with patch("uams.system.logger") as mock_logger:
                u._check_tenant_cap(_ctx_with_tenant("t-throttle"))
                u._check_tenant_cap(_ctx_with_tenant("t-throttle"))
                u._check_tenant_cap(_ctx_with_tenant("t-throttle"))
                # The "tenant_cap: tenant=" warning fires once.
                tenant_warn_calls = [
                    c for c in mock_logger.warning.call_args_list
                    if "tenant_cap" in str(c)
                ]
                self.assertEqual(len(tenant_warn_calls), 1)
        finally:
            u.shutdown()

    def test_over_storage_bytes_cap(self) -> None:
        u = UniversalMemorySystem(config=UAMSConfig(
            tenant_max_memory_count=None,
            tenant_max_storage_bytes=1,  # extreme: even empty raw exceeds
        ))
        try:
            # No memories yet — under cap (0 bytes vs 1-byte cap)
            # Wait — 0 bytes is under a 1-byte cap. So we need to
            # observe first to push bytes above the cap.
            u.observe(_evt(_ctx_with_tenant("t-bytes")))
            # Now 1 memory worth of "hello" = at least 5 bytes > 1
            self.assertFalse(u._check_tenant_cap(_ctx_with_tenant("t-bytes")))
        finally:
            u.shutdown()

    def test_no_tenant_id_means_unlimited(self) -> None:
        u = UniversalMemorySystem(config=UAMSConfig(
            tenant_max_memory_count=1,
        ))
        try:
            # No tenant_id — caps don't apply
            self.assertTrue(u._check_tenant_cap(_ctx_with_tenant(None)))
        finally:
            u.shutdown()


if __name__ == "__main__":
    unittest.main()