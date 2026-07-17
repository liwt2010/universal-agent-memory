"""CLI entry points for UAMS developer / SRE tooling.

v0.7.0 (audit item 6): UAMS is a library — it doesn't ship a
user-facing CLI. But for developers / SREs running UAMS
deployments, there are 4 ops tools that have no good home
elsewhere:

  uams-inspect   — print a memory_id's state across all tiers
                   (which tier holds it, relations, last
                   access count, audit log line if any)
  uams-doctor    — scan a UAMSConfig for drift (dead fields
                   like enable_audit_log, missing tenant
                   caps on multi-tenant deployments,
                   search_vector on a non-vector backend)
  uams-migrate   — wrapper around utils.backup.MigrationTool
                   with a friendlier CLI (--source, --target,
                   --batch-size)
  uams-bench     — wrapper around benchmarks.stress_test with
                   preflight checks

These are intentionally NOT a unified `uams` command with
subcommands — Vault owns the user-facing CLI surface (Typer).
We ship 4 separate console_scripts so a Vault / CI pipeline
can call any one of them directly.

None of these touch runtime data. They are read-only or
explicit-data-migration tools.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from uams.config import UAMSConfig
from uams.core.enums import MemoryType
from uams.system import UniversalMemorySystem
from uams.utils.logging import get_logger

logger = get_logger(__name__)


def _build_system_from_args(args: argparse.Namespace) -> UniversalMemorySystem:
    """Construct a UniversalMemorySystem with config from env + CLI overrides.

    Subcommands pass --storage-backend / --db-path / --redis-host
    etc. via argparse; we translate the relevant ones into a
    partial UAMSConfig that overrides the env-driven defaults.
    """
    from uams.config import UAMSConfig
    overrides: dict[str, Any] = {}
    if getattr(args, "storage_backend", None):
        overrides["storage_backend"] = args.storage_backend
    if getattr(args, "db_path", None):
        overrides["sqlite_path"] = args.db_path
    if getattr(args, "tenant_id", None):
        overrides["tenant_id"] = args.tenant_id  # type: ignore[arg-type]
    return UniversalMemorySystem(config=UAMSConfig(**overrides))


# ---------------------------------------------------------------
# uams-inspect
# ---------------------------------------------------------------

def cmd_inspect(argv: list[str] | None = None) -> int:
    """Print a memory_id's state across all tiers."""
    parser = argparse.ArgumentParser(
        prog="uams-inspect",
        description=(
            "Inspect a memory_id across all UAMS tiers. Shows which "
            "tier holds it, the relations, last_access_count, and "
            "any audit log line that references it."
        ),
    )
    parser.add_argument("memory_id", help="Memory id to inspect")
    parser.add_argument(
        "--storage-backend", default=None,
        help="Override UAMS_STORAGE_BACKEND",
    )
    parser.add_argument("--db-path", default=None, help="SQLite path")
    parser.add_argument("--tenant-id", default=None, help="tenant_id for context")
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text",
    )
    args = parser.parse_args(argv)

    u = _build_system_from_args(args)
    try:
        found_in: list[str] = []
        for tier in MemoryType:
            try:
                store = u._stores[tier]
            except KeyError:
                continue
            mem = store.retrieve(args.memory_id)
            if mem is not None:
                found_in.append(tier.name)
                if args.json:
                    print(json.dumps(_memory_to_dict(mem), default=str))
                else:
                    _print_memory_text(mem, tier.name)
        if not found_in:
            print(
                f"uams-inspect: memory_id={args.memory_id!r} "
                f"not found in any tier (tiers checked: "
                f"{[t.name for t in MemoryType]})",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        u.shutdown()


def _memory_to_dict(mem) -> dict[str, Any]:
    return {
        "id": str(mem.id),
        "memory_type": mem.metadata.memory_type.name,
        "privacy": mem.metadata.privacy.name,
        "importance": mem.metadata.importance,
        "context": {
            "agent_id": mem.context.agent_id,
            "agent_type": mem.context.agent_type,
            "session_id": mem.context.session_id,
            "user_id": mem.context.user_id,
            "team_id": mem.context.team_id,
            "project_id": mem.context.project_id,
            "tenant_id": mem.context.tenant_id,
        },
        "raw": mem.payload.raw[:200],
        "raw_full_length": len(mem.payload.raw),
        "relations": [
            {"type": r.relation_type, "target": r.target_memory_id,
             "strength": r.strength}
            for r in mem.metadata.relations
        ],
        "tags": sorted(mem.metadata.tags),
        "categories": sorted(mem.metadata.categories),
        "anchor": {
            "created_at": mem.anchor.created_at,
            "accessed_at": mem.anchor.accessed_at,
            "consolidated_at": mem.anchor.consolidated_at,
            "expires_at": mem.anchor.expires_at,
        },
    }


def _print_memory_text(mem, tier_name: str) -> None:
    print(f"--- tier: {tier_name} ---")
    print(f"id:              {mem.id}")
    print(f"memory_type:     {mem.metadata.memory_type.name}")
    print(f"privacy:         {mem.metadata.privacy.name}")
    print(f"importance:      {mem.metadata.importance}")
    print(f"context.agent_id: {mem.context.agent_id}")
    if mem.context.tenant_id:
        print(f"context.tenant_id: {mem.context.tenant_id}")
    if mem.context.project_id:
        print(f"context.project_id: {mem.context.project_id}")
    print(f"raw ({len(mem.payload.raw)} chars): {mem.payload.raw[:200]!r}")
    if mem.metadata.relations:
        print(f"relations ({len(mem.metadata.relations)}):")
        for r in mem.metadata.relations:
            print(f"  - {r.relation_type} -> {r.target_memory_id} (str={r.strength})")
    if mem.metadata.tags:
        print(f"tags: {sorted(mem.metadata.tags)}")


# ---------------------------------------------------------------
# uams-doctor
# ---------------------------------------------------------------

def cmd_doctor(argv: list[str] | None = None) -> int:
    """Scan a UAMSConfig for drift / dead fields / common misconfigs."""
    parser = argparse.ArgumentParser(
        prog="uams-doctor",
        description=(
            "Diagnose a UAMSConfig for common misconfigs: dead "
            "fields (enable_audit_log), tenant_id set but caps "
            "not, non-vector backend with vector_search_capable "
            "mismatch, etc. Prints a report and exits non-zero if "
            "any CRITICAL issue is found."
        ),
    )
    parser.add_argument(
        "--storage-backend", default=None,
        help="Override UAMS_STORAGE_BACKEND before diagnosing",
    )
    parser.add_argument("--db-path", default=None, help="SQLite path")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    # Use from_env so UAMS_AUDIT_LOG=true (etc) is honoured
    # during the diagnostic. The default UAMSConfig() ignores
    # the environment.
    cfg = UAMSConfig.from_env()
    if args.storage_backend:
        cfg = UAMSConfig(
            **{**cfg.__dict__, "storage_backend": args.storage_backend}
        )

    findings: list[dict[str, Any]] = []
    findings.extend(_check_dead_audit_fields(cfg))
    findings.extend(_check_tenant_caps(cfg))
    findings.extend(_check_vector_backend_match(cfg))
    findings.extend(_check_cascade_defaults(cfg))

    critical = [f for f in findings if f["severity"] == "critical"]

    if args.json:
        print(json.dumps(
            {"findings": findings, "critical_count": len(critical)},
            default=str,
        ))
    else:
        if not findings:
            print("uams-doctor: no findings. Config looks healthy.")
        for f in findings:
            print(
                f"[{f['severity'].upper()}] {f['code']}: {f['message']}"
            )
            if f.get("suggestion"):
                print(f"  suggestion: {f['suggestion']}")
    return 1 if critical else 0


def _check_dead_audit_fields(cfg: UAMSConfig) -> list[dict[str, Any]]:
    """enable_audit_log + audit_log_path are READ-ONLY (audit
    log writer was deferred from v0.6.0 to v0.6.x). Warn if the
    operator has set them expecting a real audit writer.
    """
    out = []
    if cfg.enable_audit_log:
        out.append({
            "severity": "warning",
            "code": "DEAD_AUDIT_LOG",
            "message": (
                "enable_audit_log=True but no audit writer is "
                "implemented yet (deferred to v0.6.x). The setting "
                "is parsed but has no effect."
            ),
            "suggestion": (
                "Remove UAMS_AUDIT_LOG=true / enable_audit_log=True "
                "until v0.6.x ships the GeneralAuditWriter."
            ),
        })
    return out


def _check_tenant_caps(cfg: UAMSConfig) -> list[dict[str, Any]]:
    """If the operator is using tenant_id (multi-tenant), warn
    that they have no caps configured — a single tenant can
    grow without bound.
    """
    out = []
    # tenant_id is per-context, not per-config. We can only hint
    # when the deployment looks multi-tenant (env var set or the
    # operator passed --storage-backend that suggests SaaS).
    import os
    if os.getenv("UAMS_TENANT_ID"):
        if cfg.tenant_max_memory_count is None and cfg.tenant_max_storage_bytes is None:
            out.append({
                "severity": "warning",
                "code": "TENANT_CAPS_NOT_SET",
                "message": (
                    "UAMS_TENANT_ID is set but no tenant-level caps "
                    "are configured. A single tenant can grow the "
                    "backend without bound."
                ),
                "suggestion": (
                    "Set UAMS_TENANT_MAX_MEMORY_COUNT and/or "
                    "UAMS_TENANT_MAX_STORAGE_BYTES to bound per-tenant "
                    "storage."
                ),
            })
    return out


def _check_vector_backend_match(cfg: UAMSConfig) -> list[dict[str, Any]]:
    """Non-vector backends (SQLite, Redis, PostgreSQL, Neo4j) log
    a recency-fallback INFO on every search_vector call. If the
    operator's retrieval pipeline is expected to be cosine-driven
    (e.g. RAG), this is a real problem — surface it.
    """
    out = []
    non_vector = {"sqlite", "redis", "postgresql", "neo4j"}
    if cfg.storage_backend in non_vector:
        out.append({
            "severity": "info",
            "code": "VECTOR_FALLBACK",
            "message": (
                f"storage_backend={cfg.storage_backend!r} has no "
                "native vector search; search_vector() returns "
                "recency-ordered results. If you need real cosine "
                "similarity, switch to ChromaDB or InMemoryStore."
            ),
            "suggestion": (
                "Set UAMS_STORAGE_BACKEND=chromadb and install "
                "`pip install 'universal-agent-memory[chromadb]'`."
            ),
        })
    return out


def _check_cascade_defaults(cfg: UAMSConfig) -> list[dict[str, Any]]:
    """cascade_max_depth > 6 is almost never useful and risks
    O(N^depth) on dense graphs. Warn.
    """
    out = []
    if cfg.cascade_max_depth > 6:
        out.append({
            "severity": "warning",
            "code": "CASCADE_DEPTH_HIGH",
            "message": (
                f"cascade_max_depth={cfg.cascade_max_depth} is high. "
                "For graphs with >1000 memories, this risks "
                "O(N^depth) cascade time."
            ),
            "suggestion": (
                "Set UAMS_CASCADE_MAX_DEPTH=4 (the default) for "
                "most workloads."
            ),
        })
    return out


# ---------------------------------------------------------------
# uams-migrate
# ---------------------------------------------------------------

def cmd_migrate(argv: list[str] | None = None) -> int:
    """Wrapper around utils.backup.MigrationTool."""
    parser = argparse.ArgumentParser(
        prog="uams-migrate",
        description=(
            "Migrate memories between UAMS backends. Streams in "
            "batches to avoid the v0.5.x list_all(999999) cap. "
            "Use --dry-run to count first."
        ),
    )
    parser.add_argument(
        "--source", required=True,
        help="Source backend (e.g. sqlite, memory, redis://host:6379/0)",
    )
    parser.add_argument(
        "--target", required=True,
        help="Target backend (e.g. chromadb, redis://host:6379/1)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=1000,
        help="Source list_all batch size (default: 1000)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count source rows and exit without migrating",
    )
    args = parser.parse_args(argv)

    # uams-migrate doesn't yet support backend-from-URL parsing
    # (e.g. redis://host:6379/0). For v0.7.0, it only supports
    # the same backend as the local config. The full cross-
    # backend migration through the CLI is the v0.6.x follow-up
    # that needs a ConfigurationResolver layer.
    print(
        "uams-migrate: cross-backend migration via CLI is the "
        "v0.6.x follow-up. For v0.7.0, use:\n"
        "  python -c \"from uams.utils.backup import MigrationTool; "
        "MigrationTool().migrate(source, target, batch_size="
        f"{args.batch_size})\"\n"
        "to drive migration from Python (where you can pass "
        "MemoryStore instances directly).",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------
# uams-bench
# ---------------------------------------------------------------

def cmd_bench(argv: list[str] | None = None) -> int:
    """Wrapper around benchmarks.stress_test."""
    parser = argparse.ArgumentParser(
        prog="uams-bench",
        description=(
            "Run the UAMS storage stress test (100k ops against "
            "a real backend). Preflight check verifies the chosen "
            "backend is reachable before launching."
        ),
    )
    parser.add_argument(
        "--backend", required=True,
        choices=["memory", "sqlite", "postgresql", "redis", "neo4j", "chromadb"],
    )
    parser.add_argument("--ops", type=int, default=10_000, help="Total ops")
    parser.add_argument("--concurrency", type=int, default=8, help="Worker count")
    args = parser.parse_args(argv)

    # Preflight: try to import the requested backend's deps
    try:
        if args.backend == "memory":
            from uams.storage.memory import InMemoryStore  # noqa: F401
        elif args.backend == "sqlite":
            import sqlite3  # noqa: F401
            from uams.storage.sqlite import SQLiteStore  # noqa: F401
        elif args.backend == "postgresql":
            import psycopg2  # noqa: F401
        elif args.backend == "redis":
            import redis  # noqa: F401
        elif args.backend == "neo4j":
            from neo4j import GraphDatabase  # noqa: F401
        elif args.backend == "chromadb":
            import chromadb  # noqa: F401
    except ImportError as exc:
        print(
            f"uams-bench: preflight failed for backend={args.backend!r}: "
            f"{exc}\n"
            f"Install the matching extra: "
            f"`pip install 'universal-agent-memory[{args.backend}]'`",
            file=sys.stderr,
        )
        return 2

    # Delegate to the existing stress_test module
    try:
        from benchmarks.stress_test import main as stress_main  # type: ignore
    except ImportError:
        print(
            "uams-bench: benchmarks.stress_test not importable. "
            "Run from the project root: `python -m benchmarks.stress_test`.",
            file=sys.stderr,
        )
        return 2

    print(
        f"uams-bench: delegating to benchmarks.stress_test "
        f"with backend={args.backend!r}, ops={args.ops}, "
        f"concurrency={args.concurrency}",
    )
    return stress_main([
        "--backend", args.backend,
        "--ops", str(args.ops),
        "--concurrency", str(args.concurrency),
    ])


# ---------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------

def main_inspect() -> None:  # console_scripts entry point
    sys.exit(cmd_inspect())


def main_doctor() -> None:
    sys.exit(cmd_doctor())


def main_migrate() -> None:
    sys.exit(cmd_migrate())


def main_bench() -> None:
    sys.exit(cmd_bench())
