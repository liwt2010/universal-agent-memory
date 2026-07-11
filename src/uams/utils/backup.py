"""Backup and restore utilities for UAMS storage backends.

Supports exporting and importing memory data across backends:
- Memory (InMemoryStore)
- SQLite
- Redis
- Neo4j
- PostgreSQL

Format: JSON Lines (JSONL) with one Memory per line.
"""

import json
import time
from typing import Any, Dict, List, Optional

from uams.storage.base import MemoryStore
from uams.storage.memory import InMemoryStore
from uams.core.models import Memory, MemoryId, TemporalAnchor, AgentContext, MemoryPayload, MemoryMetadata
from uams.core.enums import MemoryType, PrivacyLevel
from uams.utils.logging import get_logger

logger = get_logger(__name__)


class BackupManager:
    """Manages backup and restore operations for UAMS memory stores."""

    def __init__(self, store: MemoryStore):
        self._store = store

    def backup_to_file(self, filepath: str, limit: int = 100000) -> Optional[int]:
        """Export all memories to a JSONL file.

        Returns number of memories exported on success, or None on
        failure (an empty store returns 0, which is distinct from the
        failure signal). The exception is logged at ERROR level so the
        operator can investigate without needing to inspect return
        values.
        """
        try:
            memories = self._store.list_all(limit=limit)
            count = 0
            with open(filepath, "w", encoding="utf-8") as f:
                for mem in memories:
                    f.write(json.dumps(mem.to_json(), ensure_ascii=False) + "\n")
                    count += 1
            logger.info("Backup completed: %d memories exported to %s", count, filepath)
            return count
        except Exception:
            logger.error("Backup failed to %s", filepath, exc_info=True)
            return None

    def restore_from_file(self, filepath: str) -> Optional[int]:
        """Import memories from a JSONL file.

        Returns number of memories imported on success, or None on
        fatal failure (a file with zero valid lines returns 0; a file
        that could not be opened at all returns None).
        """
        try:
            count = 0
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        mem = Memory.from_json(data)
                        if mem:
                            self._store.store(mem)
                            count += 1
                    except Exception:
                        logger.warning("Skipped invalid backup line: %s...", line[:200])
            logger.info("Restore completed: %d memories imported from %s", count, filepath)
            return count
        except Exception:
            logger.error("Restore failed from %s", filepath, exc_info=True)
            return None

    def backup_to_dict(self, limit: int = 100000) -> List[Dict[str, Any]]:
        """Export all memories to a list of dictionaries."""
        try:
            memories = self._store.list_all(limit=limit)
            return [mem.to_json() for mem in memories]
        except Exception:
            logger.exception("Backup to dict failed")
            return []

    def restore_from_dict(self, data: List[Dict[str, Any]]) -> int:
        """Import memories from a list of dictionaries."""
        count = 0
        for item in data:
            try:
                mem = Memory.from_json(item)
                if mem:
                    self._store.store(mem)
                    count += 1
            except Exception:
                logger.warning("Skipped invalid backup item: %s...", str(item)[:200])
        logger.info("Restore from dict completed: %d memories imported", count)
        return count


class MigrationTool:
    """Migrate data between different storage backends."""

    def migrate(
        self,
        source: MemoryStore,
        target: MemoryStore,
        batch_size: int = 1000,
    ) -> int:
        """Migrate all memories from source to target. Returns count migrated."""
        logger.info("Starting migration from %s to %s", type(source).__name__, type(target).__name__)
        total = 0

        try:
            # Get all memories at once (snapshot approach to avoid pagination issues)
            all_memories = source.list_all(limit=999999999)
            logger.info("Migration snapshot: %d memories to migrate", len(all_memories))

            for i in range(0, len(all_memories), batch_size):
                batch = all_memories[i:i + batch_size]
                for mem in batch:
                    target.store(mem)
                    total += 1
                if (i // batch_size) % 10 == 0:
                    logger.info("Migration progress: %d/%d memories migrated", total, len(all_memories))
        except Exception:
            logger.exception("Migration failed")

        logger.info("Migration completed: %d memories migrated from %s to %s",
                    total, type(source).__name__, type(target).__name__)
        return total

    def migrate_with_filter(
        self,
        source: MemoryStore,
        target: MemoryStore,
        filter_fn: callable,
        batch_size: int = 1000,
    ) -> int:
        """Migrate memories matching a filter function."""
        total = 0
        try:
            all_memories = source.list_all(limit=999999999)
            filtered = [mem for mem in all_memories if filter_fn(mem)]
            for i in range(0, len(filtered), batch_size):
                batch = filtered[i:i + batch_size]
                for mem in batch:
                    target.store(mem)
                    total += 1
        except Exception:
            logger.exception("Filtered migration failed")
        logger.info("Filtered migration completed: %d memories migrated", total)
        return total


def create_backup_manager(store: MemoryStore) -> BackupManager:
    """Factory function to create a BackupManager for any store."""
    return BackupManager(store)
