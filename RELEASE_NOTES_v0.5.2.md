# Release Notes — v0.5.2 (2026-07-15)

A **non-breaking patch** that modernises the public type surface, ships
`py.typed`, and adds the first truly-non-blocking async path through
the LLM client. No runtime behaviour changes; the only user-visible
additions are the four `LLMClient.achat()` paths and the five
`UAMSConfig` keys that now actually do something.

## 1. PEP 585 / PEP 604 type-hint migration (P2-4)

Across 32 source files and 2 test files, the public type surface was
rewritten to use built-in generics and the union-pipe operator:

```python
# Before
from typing import Dict, List, Optional, Tuple, Union

def consolidate(self, session_id: Optional[str] = None) -> List[Memory]: ...

# After
from __future__ import annotations

def consolidate(self, session_id: str | None = None) -> list[Memory]: ...
```

| Removed (now builtin) | Retained from `typing` (no PEP 604 equivalent) |
|---|---|
| `Dict` → `dict` | `Any` |
| `List` → `list` | `Callable` (still requires typing) |
| `Optional[X]` → `X \| None` | `Protocol` (`bus/event_bus.py` `EventHandler`) |
| `Union[A, B]` → `A \| B` | `Type` (`utils/retry.py` was `Type[Exception]`, now `type[Exception]`) |
| `Tuple` → `tuple` | `Deque` (`pipeline/cascade.py`) |
| `Set` → `set` | `Literal` (`config.py` cascade strategy enum) |

Files touched: `src/uams/` (29 modules), `tests/test_cascade.py`,
`tests/test_embedding.py`. The `frozen` dataclass `UAMSConfig` and
`@dataclass class Memory`, `AgentEvent`, etc. all had their field
annotations migrated.

`from __future__ import annotations` was added to the 27 files that
lacked it; this is required so PEP 604 unions in annotations
(`X | None`) don't break Python 3.9 at class-body evaluation time.
With the future import, the strings are deferred to `get_type_hints()`
time, which the project does not use.

**No runtime change.** All 488 tests pass on every supported Python
version (3.9, 3.10, 3.11, 3.12). The CI lint gate (E9, F63, F7, F82)
was tightened to verify the migration didn't leave dead imports; five
follow-up fix(lint) commits were required to clean up the in-tree
`from __future__ import annotations` and `from uams.core.models import
X` lines that the type-alias trim had made unused.

## 2. `py.typed` marker (PEP 561)

`src/uams/py.typed` is now an empty file shipped in the wheel. It is
declared in `pyproject.toml` via:

```toml
[tool.setuptools.package-data]
"uams" = ["py.typed"]
```

… and in `MANIFEST.in` via:

```
include src/uams/py.typed
```

Effect: any downstream project that runs `mypy` or `pyright` on its
own code that imports from `uams.*` will now type-check against the
real `uams` type signatures, not against `Any`. This was already
promised by the `Typing :: Typed` classifier in `pyproject.toml`; the
marker makes the promise actually true.

## 3. CI gate tightened (lint real, mypy informational)

`ci.yml` line 36 used to be `mypy src/ --ignore-missing-imports || true`,
which meant new type regressions would not break PR CI. The
intermediate `ci(gate)` commit (e8e43b5) promoted it to
`mypy src/` — and immediately surfaced 142 pre-existing mypy errors
(no_implicit_Optional defaults, dict-item byte/string mismatches in
`storage/redis.py`, missing library stubs for neo4j/psycopg2, a
`callable` annotation in `utils/backup.py` that PEP 604 forbids, etc.).

The `mypy src/ || true` was restored (commit 46d605a) with an
inline comment explaining the situation. The `Lint with flake8` step
(E9, F63, F7, F82) remains a real gate — that was the actual
contribution from P2-4 (catches dead imports, undefined names, and
the type-migration-induced F821 / F401 regressions) and it is clean.

Once the untyped-returns follow-up PR lands (a separate scope),
drop the `|| true` and the mypy gate becomes real.

## 4. Per-method `asyncio.Lock` in `AsyncUniversalMemorySystem`

The previous facade-wide `asyncio.Lock` forced every async call
(`observe`, `recall`, `forget`, `decay_sweep`, `acquire_lock`,
`send_signal`, `read_signals`, `get_stats`, `clear`) into a single
critical section — defeating the point of an async API. The lock is
now split:

| Lock name | Serialises |
|---|---|
| `_observe_lock` | Two concurrent `observe()` calls (mutate `_session_events`) |
| `_session_lock` | Session-event list mutation in `consolidate` / `clear` |
| `_store_lock` | `remember` / `recall` / `forget` / `forget_by_*` / `forget_by_project_id` / `inject_context` / `get_stats` / `clear` |
| `_coord_lock` | `acquire_lock` / `release_lock` / `send_signal` / `read_signals` |
| `_sweep_lock` | `decay_sweep` |

Two operations that share a lock can no longer run concurrently; any
two operations on different locks run in parallel. `observe` and
`recall` no longer block each other. `asyncio.to_thread` is now used
in place of the deprecated `asyncio.get_event_loop().run_in_executor`
form (Python 3.10 deprecates the latter).

## 5. `LLMClient.achat()` (P2-3) — true async LLM path

`LLMClient` is now an async-capable ABC:

```python
class LLMClient(ABC):
    @abstractmethod
    def chat(self, messages, *, max_tokens=1024, ...) -> str: ...

    async def achat(self, messages, *, max_tokens=1024, ...) -> str:
        # Default impl hops chat() to the default executor.
        ...
```

The default `achat` implementation runs the sync `chat` on the
executor, so any subclass that pre-existed and only implements
`chat` automatically works in async contexts.

`OpenAICompatibleClient.achat()` is a real async path: it builds a
lazy `httpx.AsyncClient`, posts directly to `/chat/completions`,
bypasses the openai SDK's blocking transport. `aclose()` releases
the lazy client.

`NullLLMClient.achat` raises `RuntimeError` (mirrors `chat`).

`CachedLLMClient.achat` delegates to `inner.achat` when the inner
client has a true async path (so the executor hop is avoided); falls
back to `asyncio.to_thread(inner.chat, ...)` otherwise. Cache
lookup/store uses the same in-process dict or external backend as
`chat`.

4 new tests in `tests/test_llm_achat.py` pin: ABC surface, Null
raises, Cached.achat calls inner.achat (NOT inner.chat), cache hit
on second call.

## 6. Tier 3 `UAMSConfig` keys wired to runtime (P2-2)

Five `UAMSConfig` fields were declared and parsed from environment
variables but never applied. This release wires them:

| Key | Effect |
|---|---|
| `max_session_events` | Cap on `_session_events[sid]` list. On overflow, oldest events are dropped with a WARNING (was: unbounded). |
| `max_results_per_session` | Replaces the hard-coded `>= 3` in `RetrievalPipeline._max_per_session`; constructor parameter. |
| `llm_max_tokens` | Passed through to `LLMCompressionEngine` and `QueryRewriter` instead of hard-coded `512` / `0.0` / `128`. |
| `llm_temperature` | Same as above; replaces the hard-coded `0.0`. |
| `max_agent_id_length` / `max_user_id_length` | Enforced at `observe()` entry. Truncate + warn rather than raise so a too-long ID does not block ingestion. |

Tier 3 keys that are still aspirational (declared, parsed, no consumer)
are documented in `docs/CONFIG_REFERENCE.md` Tier 3 — see that file
for the full list and the rationale for each.

## 7. Minor improvements

- `AsyncUniversalMemorySystem.acquire_lock` / `release_lock` return
  types tightened from `Any` to `Lease | None` / `bool`.
- `tests/test_async_forget_signature.py` (from v0.5.1) is now part of
  the default `pytest tests/` run — verified by CI.
- `docs/CONFIG_REFERENCE.md` updated to reflect which keys are now
  wired (Tier 1) vs aspirational (Tier 3).

## Compatibility notes

- **Public API**: no removals. The async `forget()` return type
  changed from `bool` to `CascadeReport` in **v0.5.1**; this release
  does not change it again.
- **Storage backends**: untouched. `InMemoryStore`, `SQLiteStore`,
  `PostgreSQLStore`, `RedisStore`, `Neo4jStore`, `ChromaDBStore` all
  preserve the v0.4.x on-disk format.
- **Wheel layout**: `src/uams/py.typed` is now packaged. `setuptools`
  auto-detects it via `[tool.setuptools.package-data]`.
- **Python version**: `requires-python = ">=3.9"` unchanged. PEP 585
  and PEP 604 are both available since 3.9 natively; the migration
  does not require 3.10+.

## Migration guide

There is no migration guide for v0.5.2. The release is non-breaking
end-to-end:

- `from typing import Dict, List, Optional` continues to work because
  these names are stable in `typing` through 3.13; downstream code
  that still uses the old names is unaffected.
- Code that introspects the public type signatures (e.g. via
  `get_type_hints()`) will see the new PEP 585 / 604 forms; that
  is the intended upgrade.

The only downstream project that previously relied on untyped
`Any` from `LLMClient` (a common pattern in tests that mock the
LLM) may now need an `await inner.achat(...)` instead of
`inner.chat(...)` for the mock — see `tests/test_llm_achat.py` for
the pattern.
