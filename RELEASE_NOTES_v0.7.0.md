# v0.7.0 Release Notes

**Non-breaking minor release.** Closes the four Vault-product-
layer audit items that landed between v0.6.0 and v0.6.x. Adds
the long-promised `uams.extract.auto_extract` single-call API,
4 dev/SRE CLI entry points, local-LLM auto-detection, and
tenant-level resource caps.

## What's in this release

### New public surface

* **`uams.extract.auto_extract(system, conversation, **kwargs)`** —
  the end-to-end single-call API Vault was previously building
  by hand (manually call `observe()` for each message, then
  `consolidate()`). v0.7.0 ships this as a first-class library
  call.

  ```python
  from uams.extract import auto_extract

  result = auto_extract(
      uams_system,
      "I prefer boutique hotels and Mediterranean food",
      agent_id="alice",
      agent_type="personal_assistant",
      session_id="sess-1",
      user_id="alice",
      project_id="vault",
      tenant_id="acme",
  )
  # result.episodic: episodic memory for the conversation
  # result.semantic_facts: list of atomic-fact memories
  ```

* **`uams.extract.AutoExtractResult`** (dataclass) — typed
  return value.
* **`uams.cli`** (new module) — 4 dev/SRE entry points:
  - `uams-inspect <memory_id>` — print a memory's state across
    all tiers.
  - `uams-doctor` — config-drift scanner.
  - `uams-migrate` — wrapper around `MigrationTool`.
  - `uams-bench` — wrapper around `benchmarks.stress_test`.

  These are 4 separate console_scripts (not a unified `uams`
  command with subcommands). Vault owns the user-facing CLI
  surface (Typer). The 4 entry points are for developers / SRE
  / CI use. None of them touch runtime data — read-only or
  explicit-data-migration tools.

* **`UAMSConfig.from_env_with_local_auto_detect()`** —
  new classmethod that probes well-known local LLM endpoints
  and switches `llm_base_url` + `llm_provider` to whichever
  responds first.

* **`UAMSConfig.tenant_max_memory_count`** + **`tenant_max_storage_bytes`** + **`hard_enforce_tenant_caps`** —
  new soft caps on per-tenant memory count and storage bytes.
  `None` by default (no cap). `observe()` logs a WARNING the
  first time the cap is exceeded per process lifetime
  (throttled).

### Behaviour changes

* `OpenAICompatibleClient` accepts empty `api_key` (replaced
  with the `'ollama'` placeholder convention).
* `UniversalMemorySystem.observe()` calls `_check_tenant_cap(ctx)`
  at entry. In `hard_enforce_tenant_caps` mode, over-cap events
  are dropped (logged); in default warn-only mode, they
  continue through the normal pipeline.

### Compatibility

* No public API removals.
* `auto_extract` accepts plain string or list of
  `{role, content}` dicts. Bad input raises
  `TypeError` / `ValueError` before any side effect.
* `from_env_with_local_auto_detect` is opt-in; callers using
  `from_env` directly see no behaviour change.
* `tenant_*_caps` keys default to `None`; deployments that
  don't set them see no behaviour change.

## Migration from v0.6.0

For most users, `pip install --upgrade universal-agent-memory`
is all that's needed. To use the new features:

```python
# Old: hand-rolled observe + consolidate
for m in messages:
    uams.observe(AgentEvent(content=m, ...))
result = uams.consolidate(session_id)

# New: one call
from uams.extract import auto_extract
result = auto_extract(uams, messages, agent_id=..., session_id=...)
```

```bash
# New CLI tools (after pip install)
uams-inspect <memory_id>     # see which tier holds a memory
uams-doctor                  # check config for drift
uams-migrate --source X --target Y
uams-bench --backend sqlite --ops 10000
```

```python
# New: enable multi-tenant resource caps
config = UAMSConfig(
    tenant_id="acme",
    tenant_max_memory_count=100_000,  # warn-only by default
    hard_enforce_tenant_caps=False,
)
```

```python
# New: opt into local LLM auto-detection
config = UAMSConfig.from_env_with_local_auto_detect()
# Probes localhost:11434 (ollama), :1234 (LM Studio), :8000 (vLLM)
# and switches to whichever responds first
```

## See also

* `CHANGELOG.md` — same release from a "what changed" perspective
* `PRODUCTION_ASSESSMENT.md` v11 — production-readiness rating
* `docs/CONFIG_REFERENCE.md` — updated Tier 1 for the new
  config keys
* `docs/ARCHITECTURE.md` — `auto_extract` flows through the
  existing `observe` + `consolidate` pipeline; see the
  "Engine-level capabilities" section
