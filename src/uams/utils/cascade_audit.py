"""Append-only JSONL audit log writer for cascade-forget events.

Per spec sec 11 (audit log format):
- Two files: cascade_forget_audit.jsonl (per-invocation) and
  cascade_orphan_log.jsonl (per-cross-tier-edge-orphan).
- Thread-safe via RLock so concurrent forget() calls in one process
  don't interleave bytes.
- Lazy dir creation; flush on append; no fsync by design (sec 11.3).
"""

from __future__ import annotations

import json
import threading
from typing import Any

from pathlib import Path


class CascadeAuditWriter:
    """Thread-safe append-only JSONL writer for cascade-forget records."""

    def __init__(
        self,
        path: Path | str = "logs/cascade_forget_audit.jsonl",
        orphan_path: Path | str = "logs/cascade_orphan_log.jsonl",
    ) -> None:
        self._path = Path(path)
        self._orphan_path = Path(orphan_path)
        self._lock = threading.RLock()
        self._fp = None
        self._orphan_fp = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def orphan_path(self) -> Path:
        return self._orphan_path

    def _ensure_open(self, which: str) -> None:
        # which in {"main", "orphan"}
        attr = "_fp" if which == "main" else "_orphan_fp"
        fp = getattr(self, attr)
        if fp is not None:
            return
        path = self._path if which == "main" else self._orphan_path
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a", encoding="utf-8", newline="\n")
        setattr(self, attr, f)

    def append(self, record: dict[str, Any]) -> None:
        """Write one record as a single JSON line."""
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._ensure_open("main")
            self._fp.write(line + "\n")
            self._fp.flush()

    def append_orphan(self, record: dict[str, Any]) -> None:
        """Write one orphan-edge record. Independent file from main."""
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._ensure_open("orphan")
            self._orphan_fp.write(line + "\n")
            self._orphan_fp.flush()

    def close(self) -> None:
        with self._lock:
            for attr in ("_fp", "_orphan_fp"):
                fp = getattr(self, attr)
                if fp is not None:
                    fp.close()
                    setattr(self, attr, None)
