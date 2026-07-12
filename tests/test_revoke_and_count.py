"""Tests for revoke_agent / revoke_project / delete_by_project_id.

Bug 4: previously these were missing from the public API, so callers
(Vault) had to do list_all() + filter + delete() themselves — an O(N)
wire scan. The new methods are thin wrappers over
``MemoryStore.delete_by_filter()`` which is O(matches) on SQLite / PG
(flat indexed columns) and best-effort O(N) on Redis / ChromaDB /
Neo4j.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_mem(memory_id, agent_id, project_id, tier="semantic"):
    from uams.core.enums import MemoryType, PrivacyLevel
    from uams.core.models import (
        AgentContext, Memory, MemoryId, MemoryMetadata,
        MemoryPayload, TemporalAnchor,
    )
    return Memory(
        id=MemoryId(memory_id),
        anchor=TemporalAnchor(),
        context=AgentContext(
            agent_id=agent_id, agent_type="t", session_id="s",
            project_id=project_id,
        ),
        payload=MemoryPayload(raw=f"raw-{memory_id}"),
        metadata=MemoryMetadata(MemoryType.SEMANTIC, PrivacyLevel.PUBLIC),
    )


class TestRevokeAgent(unittest.TestCase):
    def test_revoke_agent_deletes_only_matching(self):
        """Memories with matching agent_id are removed; others stay."""
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType

        ums = UniversalMemorySystem()
        store = ums._stores[MemoryType.SEMANTIC]
        store.store(_make_mem("a-1", "alice", "proj-1"))
        store.store(_make_mem("a-2", "alice", "proj-2"))
        store.store(_make_mem("b-1", "bob",   "proj-1"))

        deleted = ums.revoke_agent("alice")
        self.assertEqual(deleted, 2)
        # bob's memory survives.
        self.assertIsNotNone(store.retrieve("b-1"))
        self.assertIsNone(store.retrieve("a-1"))
        self.assertIsNone(store.retrieve("a-2"))

    def test_revoke_agent_returns_zero_on_no_match(self):
        from uams import UniversalMemorySystem
        ums = UniversalMemorySystem()
        self.assertEqual(ums.revoke_agent("nobody"), 0)


class TestRevokeProject(unittest.TestCase):
    def test_revoke_project_deletes_only_matching(self):
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType

        ums = UniversalMemorySystem()
        store = ums._stores[MemoryType.SEMANTIC]
        store.store(_make_mem("p-1", "alice", "proj-x"))
        store.store(_make_mem("p-2", "bob",   "proj-x"))
        store.store(_make_mem("p-3", "alice", "proj-y"))

        deleted = ums.revoke_project("proj-x")
        self.assertEqual(deleted, 2)
        self.assertIsNotNone(store.retrieve("p-3"))
        self.assertIsNone(store.retrieve("p-1"))
        self.assertIsNone(store.retrieve("p-2"))


class TestDeleteByProjectId(unittest.TestCase):
    def test_basic_delete_by_project_id(self):
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType

        ums = UniversalMemorySystem()
        store = ums._stores[MemoryType.SEMANTIC]
        store.store(_make_mem("d-1", "alice", "kill-me"))
        store.store(_make_mem("d-2", "alice", "keep"))
        deleted = ums.delete_by_project_id("kill-me")
        self.assertEqual(deleted, 1)
        self.assertIsNone(store.retrieve("d-1"))
        self.assertIsNotNone(store.retrieve("d-2"))

    def test_tenant_id_filter_narrows_to_intersection(self):
        """When tenant_id is given, only memories matching BOTH project_id
        AND tenant_id are deleted."""
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType
        from uams.core.models import (
            AgentContext, Memory, MemoryId, MemoryMetadata,
            MemoryPayload, TemporalAnchor,
        )
        from uams.core.enums import MemoryType as MT, PrivacyLevel as PL

        def mem_w_tenant(mid, agent, proj, tenant):
            return Memory(
                id=MemoryId(mid),
                anchor=TemporalAnchor(),
                context=AgentContext(
                    agent_id=agent, agent_type="t", session_id="s",
                    project_id=proj, tenant_id=tenant,
                ),
                payload=MemoryPayload(raw=f"r-{mid}"),
                metadata=MemoryMetadata(MT.SEMANTIC, PL.PUBLIC),
            )

        ums = UniversalMemorySystem()
        store = ums._stores[MemoryType.SEMANTIC]
        store.store(mem_w_tenant("t-1", "alice", "P", "tenant-A"))
        store.store(mem_w_tenant("t-2", "alice", "P", "tenant-B"))

        deleted = ums.delete_by_project_id("P", tenant_id="tenant-A")
        # Only t-1 (tenant-A) deleted; t-2 survives because tenant_id
        # doesn't match.
        self.assertEqual(deleted, 1)
        self.assertIsNone(store.retrieve("t-1"))
        self.assertIsNotNone(store.retrieve("t-2"))


class TestCountAndGetStats(unittest.TestCase):
    def test_count_returns_zero_on_empty(self):
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType
        ums = UniversalMemorySystem()
        for store in ums._stores.values():
            self.assertEqual(store.count(), 0)

    def test_count_after_storing(self):
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType
        ums = UniversalMemorySystem()
        store = ums._stores[MemoryType.SEMANTIC]
        for i in range(5):
            store.store(_make_mem(f"c-{i}", "alice", "P"))
        self.assertEqual(store.count(), 5)

    def test_get_stats_uses_count_not_list_all(self):
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType
        ums = UniversalMemorySystem()
        store = ums._stores[MemoryType.SEMANTIC]
        for i in range(3):
            store.store(_make_mem(f"s-{i}", "alice", "P"))
        stats = ums.get_stats()
        # The SEMANTIC tier should reflect the count, not the old
        # O(N) list_all() path.
        self.assertEqual(stats["SEMANTIC"], 3)

    def test_get_stats_scan_limit_caps_count(self):
        from uams import UniversalMemorySystem
        from uams.core.enums import MemoryType
        ums = UniversalMemorySystem()
        store = ums._stores[MemoryType.SEMANTIC]
        for i in range(10):
            store.store(_make_mem(f"l-{i}", "alice", "P"))
        # scan_limit=5 caps the reported value even though actual count is 10.
        stats = ums.get_stats(scan_limit=5)
        self.assertEqual(stats["SEMANTIC"], 5)


if __name__ == "__main__":
    unittest.main()