# v0.5.3 / v0.6.0 — Audit Pass Resolution

## Context

External audit agent produced a 14-item P0/P1/P2/P3 finding list
against UAMS v0.5.2. User approved all 14 fixes + 3 modifications:
- P0-4 → "facade-only raise" (not store-wide) to preserve graceful
  degradation in `RedisStore` / `ChromaDBStore`
- P2-5 → "真做 GeneralAuditWriter" (not warning / not delete) — closing
  the existing `enable_audit_log` lie
- P0-2 → also batch-stream `MigrationTool.migrate()` and
  `BackupManager.backup_to_dict` (same `list_all(999999)` bug, audit
  mentioned only `clear()`)
- All 14 items in one push (no PR split)

P3 items (mypy strict / async facade dedup / tenant_id roundtrip
tests) deferred to v0.6.x.

## Audit items → Trace ID

| Trace | Item | Audit text |
|---|---|---|
| T01 | P0-1 | tenant_id 没真写入 6 stores |
| T02 | P0-2 | clear()/MigrationTool.migrate()/backup_to_dict 走 list_all 999 坑 |
| T03 | P0-3 | cascade reverse_index 全无 |
| T04 | P0-4 | UAMSError 异常族 + facade-only raise |
| T05 | P1-1 | ollama validator 拒但 docstring 说支持 |
| T06 | P1-2 | search_vector 行为不一致 → vector_search_capable flag |
| T07 | P1-3 | PrivacyFilter PUBLIC 不生效 → secret 永远 redact |
| T08 | P1-4 | retrieval_score 0.0 falsy |
| T09 | P1-5 | cascade _locate_tier 全 tier 扫 |
| T10 | P1-6 | ChromaDB list_all()=[] |
| T11 | P1-7 | LLM 压缩 prompt 无隐私级别指示 |
| T12 | P2-1 | delete_by_project_id 跨后端集成测试 |
| T13 | P2-3 | namespace() 不含 tenant_id |
| T14 | P2-4 | achat 无重试 |
| T15 | P2-5 | enable_audit_log 死字段 → 真做 GeneralAuditWriter |
| T16 | P2-6 | agent_type 必填不校验 |

## Version decision: v0.6.0 (minor)

- P0-1 (multi-tenant GDPR) = new capability surface → minor bump
- T15 (GeneralAuditWriter) = new feature → minor bump
- The rest = correctness / perf / DX = could be patch, but mixing
  patch + minor in one release is worse than one minor bump

## Critical files

| Trace | Touched files |
|---|---|
| T01 | `core/models.py`, `system.py`, `storage/{base,memory,sqlite,redis,postgresql,neo4j,chromadb}.py`, `tests/test_revoke_and_count.py` (extend), `tests/test_delete_by_project_id_tenant_id.py` (new) |
| T02 | `storage/base.py` (new `truncate`), `storage/{sqlite,redis,postgresql,neo4j,chromadb,memory}.py`, `system.py`, `utils/backup.py` (migrate + backup_to_dict), `tests/test_clear_streaming.py` (new) |
| T03 | `storage/{sqlite,redis,chromadb,neo4j}.py` (new `_incoming_index`), `pipeline/cascade.py`, `tests/test_cascade_reverse_index.py` (new) |
| T04 | `errors.py` (new), `system.py`, `pipeline/cascade.py`, `tests/test_errors.py` (new) |
| T05 | `config.py`, `tests/test_ollama_validator.py` (new) |
| T06 | `storage/base.py` (new class attr), `storage/{sqlite,redis,postgresql,neo4j}.py` (info-log fallback), `tests/test_vector_search_capable.py` (new) |
| T07 | `pipeline/privacy.py`, `tests/test_privacy_public_level.py` (new) |
| T08 | `pipeline/retrieval.py`, `tests/test_retrieval_score_zero.py` (new) |
| T09 | `storage/base.py` (new `find_tier`), `storage/*.py`, `pipeline/cascade.py`, `tests/test_cascade_find_tier.py` (new) |
| T10 | `storage/chromadb.py`, `tests/test_chromadb_list_all.py` (new) |
| T11 | `pipeline/llm_compression.py`, `tests/test_llm_compression_pii.py` (new) |
| T12 | `tests/test_delete_by_project_id_tenant_id.py` (counts as T01's integration test) |
| T13 | `core/models.py`, `tests/test_namespace.py` (new) |
| T14 | `llm/client.py`, `tests/test_achat_retry.py` (new) |
| T15 | `utils/general_audit.py` (new), `system.py`, `config.py`, `tests/test_general_audit.py` (new) |
| T16 | `system.py`, `tests/test_observe_validation.py` (new) |

## Order of execution (P0 first, then P1, then P2, then docs)

Each trace = one commit. Commit message includes `T<NN>:` prefix
for downstream traceability.

1. T04 errors.py (foundational — T01, T09, T15 may raise from it)
2. T08 retrieval_score 0.0 (1-line, sets the tone)
3. T05 ollama validator (1-line)
4. T01 tenant_id 6 stores (heaviest)
5. T02 clear/migrate/backup truncate (touches same files as T01)
6. T10 ChromaDB list_all (enables T12)
7. T06 vector_search_capable flag (parallel with T02)
8. T09 cascade find_tier
9. T03 cascade reverse_index
10. T07 PrivacyFilter secret
11. T11 LLM compression PII
12. T13 namespace()
13. T14 achat retry
14. T15 GeneralAuditWriter
15. T16 observe validate
16. T12 integration tests
17. CHANGELOG / RELEASE_NOTES / PRODUCTION_ASSESSMENT / CONFIG_REFERENCE
18. README 三语 alignment
19. Version bump 0.5.2 → 0.6.0
20. dist rebuild
21. tag v0.6.0
22. doc-audit
23. push

## Verification per trace

For each trace, before commit:
- `pytest tests/test_<related>.py -v` — passes (including new regression)
- `flake8 src/uams/ tests/test_<related>.py` — clean
- `mypy src/uams/ <new-or-changed-files> 2>&1 | grep -v 'note:\|hint:' | head -5` — no NEW errors

For final commit:
- `pytest tests/ -q --ignore=tests/test_postgresql_store.py --ignore=tests/test_redis_store_real.py --ignore=tests/test_neo4j_store_real.py --ignore=tests/test_chromadb_store.py` — passes
- `pytest tests/test_postgresql_store.py tests/test_redis_store_real.py tests/test_neo4j_store_real.py tests/test_chromadb_store.py` — pre-existing failures status unchanged
- `python -c "from uams import __version__; print(__version__)"` — `0.6.0`
- `python -c "from uams.errors import UAMSError, ConfigError, StorageError, CascadeError, LLMError; print('ok')"` — works
- `twine check dist/universal_agent_memory-0.6.0*` — clean

## Risk register

| Risk | Mitigation |
|---|---|
| T01 tenant_id 6-store migration breaks existing data | Add `tenant_id` column with default `None`; back-compat read. Old data falls into "no tenant" bucket and is still deleteable by `delete_by_project_id` without tenant_id filter |
| T02 truncate() vs existing clear() callers | Keep clear() as façade; truncate() is the new internal primitive. Public API surface unchanged |
| T03 reverse_index storage cost | Only store IDs, not full Memory. ~10 bytes per edge. Acceptable for v0.6.x |
| T04 facade-only raise breaks Vault try/except | Vault was already wrapping `except Exception`; adding `except UAMSError` is strictly additive |
| T15 audit writer log file ownership | Use `Path.parent.mkdir(parents=True, exist_ok=True)` + `os.chmod(0o600)` |
| T14 tenacity on httpx swallows real errors | Set `retry_if_exception_type=(httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError)` — NOT generic `Exception` |

## Stop conditions

- Any single trace failing its regression test → fix before next
- `pytest` total pass count drops below v0.5.2 baseline of 448 + new tests → rollback trace
- New mypy errors > 5 per trace → flag, ask user