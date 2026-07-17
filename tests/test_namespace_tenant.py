"""Regression test for T13 (P2-3): AgentContext.namespace()
includes tenant_id.

Pins:
- Two contexts that differ only in tenant_id produce different
  namespaces (multi-tenant isolation works at the key level)
- A context with tenant_id=None produces the same namespace as
  the pre-v0.6.0 form (back-compat — no tenant means the empty
  tenant segment collapses to '_')
- The full namespace string is stable: agent_id:user_id:_:tenant
"""

from __future__ import annotations

import unittest

from uams import AgentContext


class TestNamespaceTenant(unittest.TestCase):
    def test_different_tenants_different_namespaces(self) -> None:
        a = AgentContext(
            agent_id="agent1", agent_type="t", session_id="s",
            user_id="u", tenant_id="tenant-a",
        )
        b = AgentContext(
            agent_id="agent1", agent_type="t", session_id="s",
            user_id="u", tenant_id="tenant-b",
        )
        self.assertNotEqual(a.namespace(), b.namespace())

    def test_none_tenant_falls_back_to_underscore(self) -> None:
        ctx = AgentContext(
            agent_id="agent1", agent_type="t", session_id="s",
            user_id="u", tenant_id=None,
        )
        self.assertEqual(ctx.namespace(), "agent1:u:_:_")

    def test_all_fields_propagate(self) -> None:
        ctx = AgentContext(
            agent_id="a1", agent_type="t", session_id="s",
            user_id="u1", team_id="team-x", tenant_id="t1",
        )
        self.assertEqual(ctx.namespace(), "a1:u1:team-x:t1")

    def test_missing_user_id_falls_back(self) -> None:
        ctx = AgentContext(
            agent_id="a1", agent_type="t", session_id="s",
            user_id=None, tenant_id="t1",
        )
        self.assertEqual(ctx.namespace(), "a1:_:_:t1")


if __name__ == "__main__":
    unittest.main()