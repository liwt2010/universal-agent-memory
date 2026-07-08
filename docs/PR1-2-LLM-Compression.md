# PR1-2 LLM Compression 实装 — Handoff 文档

> **目标**:用 LLM 替代 `HeuristicCompressionEngine`,把会话历史真正压缩,显著降低 Token 消耗
> **工期**:半天-1 天(写代码)+ 半天(测试)
> **依赖**:已合 PR1-4+5(config 校验 + 作者信息)
> **不破坏**:已通过的 132 测试,默认行为不变(开发模式仍走 Heuristic)

---

## 1. 30 秒读完

1. **为什么做**:`HeuristicCompressionEngine` 几乎不压缩 token(`compression.py:35-134`,只是结构化整理)。LLM Compression 是 PR1 中 ROI 最高的一项。
2. **核心改动**:5 个文件 + 1 个新模块
3. **关键技术**:OpenAI SDK + `base_url` 可配置(兼容 MiniMax / ollama / vllm)
4. **降级策略**:LLM 失败 → 自动 fallback 到 `HeuristicCompressionEngine`,不阻塞系统
5. **完成标志**:Mock 测试 100% 覆盖 + 真实 LLM 端到端跑通 + Token 压缩比 ≥ 30%

---

## 2. 上下文(避免重复做)

### 2.1 已完成项(不要重做)
- **PR1-4**:`config.py` 加 production safety / environment 阶梯 / 27 个新测试
- **PR1-5**:`pyproject.toml` authors、`SECURITY.md`、`README.md` 维护承诺
- **132 测试全过**(105 原 + 27 新 config)

### 2.2 现有 `CompressionEngine` 接口(`pipeline/compression.py:11-33`)
```python
class CompressionEngine(ABC):
    @abstractmethod
    def compress_working_to_episodic(self, events: List[AgentEvent]) -> Memory: ...
    @abstractmethod
    def extract_semantic(self, episodic: Memory) -> List[Memory]: ...
    @abstractmethod
    def extract_procedural(self, episodes: List[Memory]) -> List[Memory]: ...
```

### 2.3 现有 `HeuristicCompressionEngine`(`compression.py:35-134`)
朴素实现:episodic 是 `[TYPE] content\n...` 拼接,semantic 只取 `(str/int/float/bool)` 字段,procedural 按 category 计数。**基本不压 token**,PR1-2 要替换它。

### 2.4 现有 `system.py` 注入点(`system.py:76`)
```python
self._compression = compression or HeuristicCompressionEngine()
```
PR1-2 改这一行:根据 `config.llm_enabled` 选择 LLMCompression 或 Heuristic。

### 2.5 现有 `UAMSConfig` 字段(`config.py`)
frozen=True dataclass,新增字段直接追加即可,不影响向后兼容。

---

## 3. 关键技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM 客户端 | **OpenAI SDK + `base_url` 可配** | 兼容 MiniMax / ollama / vllm,生态最广 |
| Anthropic 支持 | **本次不实装** | 单独 SDK,先 OpenAI 兼容覆盖 80% 场景 |
| Fallback 策略 | **LLM 失败 → 自动 Heuristic,日志 WARNING** | 系统不能因 LLM 故障挂掉 |
| Prompt 模板 | **内置 + 预留 `prompt_overrides` 字段(本次不实现 override)** | 默认够用,留扩展点 |
| 缓存 | **`CachedLLMClient` 包裹,基于 (messages, kwargs) SHA-256** | 防重复 summary 烧 token |
| 批量 | **一次 LLM call 处理 N events**(`llm_compression_max_events`) | 减少 call 数,省 token + latency |
| LLM 默认关闭 | **`llm_enabled=False` 默认走 Heuristic** | 开发模式不烧 token,生产显式开启 |

---

## 4. 模块拆分(独立可 revert)

```
src/uams/llm/                       ← 新目录
  ├── __init__.py                    ← 暴露 4 个类
  └── client.py                      ← LLMClient ABC + 3 个实现

src/uams/pipeline/
  └── llm_compression.py             ← 新文件,继承 CompressionEngine

src/uams/config.py                   ← 加 13 个 LLM 字段 + 校验
src/uams/system.py                   ← 改 __init__ 注入点(1 行 + 10 行)
tests/test_llm_compression.py        ← 新文件,~150 行 mock 测试
pyproject.toml                       ← 加 openai>=1.0 到 [llm] optional deps
```

---

## 5. 接口骨架(可直接复用)

### 5.1 `src/uams/llm/client.py`(完整骨架)

```python
"""LLM client abstraction with OpenAI-compatible implementation."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import hashlib
import logging
import threading

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """Abstract LLM client. Implementations live in this module."""

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> str:
        """Send a chat completion request and return the assistant message content."""

    def is_available(self) -> bool:
        """Return True if this client can serve requests."""
        return True


class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible client. Works with OpenAI / MiniMax / ollama / vLLM.

    Set ``base_url`` to the provider's OpenAI-compatible endpoint
    (e.g. ``https://api.minimaxi.com/v1`` for MiniMax, ``http://localhost:11434/v1`` for ollama).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "openai package required. Install: pip install 'universal-agent-memory[llm]'"
            ) from exc
        if not api_key:
            raise ValueError("api_key is required for OpenAICompatibleClient")
        self._client = OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        self._model = model

    def chat(self, messages, *, max_tokens=1024, temperature=0.0, timeout=30.0):
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        return (resp.choices[0].message.content or "")


class NullLLMClient(LLMClient):
    """Always raises. Sentinel for tests and forced fallback."""

    def chat(self, messages, **kwargs):
        raise RuntimeError("NullLLMClient cannot serve requests")


class CachedLLMClient(LLMClient):
    """Wraps another client with a (messages, kwargs) → response cache.

    Bounded LRU to avoid unbounded memory growth. Thread-safe.
    """

    def __init__(self, inner: LLMClient, max_entries: int = 1000):
        self._inner = inner
        self._max = max_entries
        self._cache: Dict[str, str] = {}
        self._lock = threading.RLock()

    def chat(self, messages, **kwargs):
        key = hashlib.sha256(
            f"{self._inner.__class__.__name__}|{repr(messages)}|{sorted(kwargs.items())}".encode()
        ).hexdigest()
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        result = self._inner.chat(messages, **kwargs)
        with self._lock:
            if len(self._cache) >= self._max:
                # Drop oldest (insertion-ordered dict semantics)
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    def is_available(self) -> bool:
        return self._inner.is_available()
```

### 5.2 `src/uams/llm/__init__.py`
```python
"""LLM client implementations for UAMS."""
from uams.llm.client import (
    CachedLLMClient,
    LLMClient,
    NullLLMClient,
    OpenAICompatibleClient,
)

__all__ = ["LLMClient", "OpenAICompatibleClient", "NullLLMClient", "CachedLLMClient"]
```

### 5.3 `src/uams/pipeline/llm_compression.py`(骨架)

```python
"""LLM-based memory compression engine.

Provides ``LLMCompressionEngine`` which uses an injected ``LLMClient``
to summarize raw events into episodic narratives, extract atomic semantic
facts, and identify recurring procedural patterns.

Falls back gracefully if the LLM client raises: logs a warning and
returns an empty / minimal result so the calling pipeline continues.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from uams.core.enums import EventType, MemoryType, PrivacyLevel
from uams.core.models import (
    AgentEvent,
    Memory,
    MemoryId,
    MemoryMetadata,
    MemoryPayload,
    TemporalAnchor,
)
from uams.llm.client import LLMClient, NullLLMClient
from uams.pipeline.compression import CompressionEngine

logger = logging.getLogger(__name__)


# --- Prompt templates (English; users can override via future field) ---

_EPISODIC_SYSTEM = (
    "You are a memory consolidation assistant. Given a chronological list of "
    "agent events from one session, produce a single concise narrative "
    "(<= 200 words) capturing the user's goals, decisions, and outcomes. "
    "Preserve concrete facts (names, dates, numbers, preferences). "
    "Output ONLY the narrative text, no preamble."
)

_EPISODIC_USER_TEMPLATE = (
    "Agent context: agent_id={agent_id}, user_id={user_id}, session_id={session_id}\n\n"
    "Events (chronological):\n{events}\n\n"
    "Narrative summary:"
)

_SEMANTIC_SYSTEM = (
    "You are a fact extractor. Given a session narrative, extract atomic "
    "facts about the user (preferences, traits, biographical data). "
    "Return a JSON array of objects: [{\"key\": <short_key>, \"value\": <string>}]. "
    "Skip transient or session-specific info. Output ONLY the JSON array."
)

_PROCEDURAL_SYSTEM = (
    "You are a workflow analyzer. Given multiple session summaries, identify "
    "recurring workflows or interaction patterns. Return a JSON array: "
    "[{\"pattern\": <short_name>, \"description\": <one sentence>, \"frequency\": <int>}]. "
    "Only include patterns observed in >= 2 sessions. Output ONLY the JSON array."
)


class LLMCompressionEngine(CompressionEngine):
    """LLM-backed compression engine. Inherits CompressionEngine contract."""

    def __init__(
        self,
        llm_client: LLMClient,
        max_events_per_call: int = 20,
        target_ratio: float = 0.3,
        timeout: float = 30.0,
    ):
        self._llm = llm_client
        self._max_events = max(1, int(max_events_per_call))
        self._target_ratio = target_ratio
        self._timeout = timeout

    # --- Episodic: events → narrative Memory ---

    def compress_working_to_episodic(self, events: List[AgentEvent]) -> Memory:
        if not events:
            raise ValueError("No events to compress")

        first = events[0]
        ctx = first.agent_context

        # Batch if too many events
        if len(events) <= self._max_events:
            narrative = self._summarize_batch(events)
        else:
            # First summarize each chunk, then summarize the summaries
            chunk_summaries = []
            for i in range(0, len(events), self._max_events):
                chunk = events[i : i + self._max_events]
                chunk_summaries.append(self._summarize_batch(chunk))
            pseudo_events = [
                AgentEvent(
                    event_type=EventType.SESSION_END,
                    agent_context=ctx,
                    content=s,
                    timestamp=events[i].timestamp if i < len(events) else time.time(),
                )
                for i, s in enumerate(chunk_summaries)
            ]
            narrative = self._summarize_batch(pseudo_events)

        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(
                created_at=events[0].timestamp,
                consolidated_at=time.time(),
            ),
            context=ctx,
            payload=MemoryPayload(
                raw=narrative,
                structured={
                    "event_count": len(events),
                    "duration_sec": events[-1].timestamp - events[0].timestamp,
                    "compression_engine": "llm",
                },
            ),
            metadata=MemoryMetadata(
                memory_type=MemoryType.EPISODIC,
                privacy=first.privacy,
                source_event=EventType.SESSION_END,
            ),
        )

    def _summarize_batch(self, events: List[AgentEvent]) -> str:
        """Call LLM to summarize a batch of events. Fallback to heuristic on error."""
        try:
            events_text = "\n".join(
                f"[{e.timestamp:.0f}|{e.event_type.name}] {e.content}" for e in events
            )
            ctx = events[0].agent_context
            user_msg = _EPISODIC_USER_TEMPLATE.format(
                agent_id=ctx.agent_id,
                user_id=ctx.user_id or "_",
                session_id=ctx.session_id,
                events=events_text,
            )
            return self._llm.chat(
                [
                    {"role": "system", "content": _EPISODIC_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=512,
                temperature=0.0,
                timeout=self._timeout,
            ).strip()
        except Exception:
            logger.exception("LLM episodic summarization failed; using raw concatenation fallback")
            return "\n".join(f"[{e.event_type.name}] {e.content}" for e in events)

    # --- Semantic: episodic narrative → atomic facts ---

    def extract_semantic(self, episodic: Memory) -> List[Memory]:
        try:
            raw = self._llm.chat(
                [
                    {"role": "system", "content": _SEMANTIC_SYSTEM},
                    {"role": "user", "content": episodic.payload.raw},
                ],
                max_tokens=512,
                temperature=0.0,
                timeout=self._timeout,
            ).strip()
            facts_json = self._parse_json_array(raw)
            facts: List[Memory] = []
            for item in facts_json:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "")).strip()
                value = str(item.get("value", "")).strip()
                if not key or not value:
                    continue
                facts.append(
                    Memory(
                        id=MemoryId(),
                        anchor=TemporalAnchor(created_at=time.time()),
                        context=episodic.context,
                        payload=MemoryPayload(
                            raw=f"{key} = {value}",
                            structured={"key": key, "value": value},
                        ),
                        metadata=MemoryMetadata(
                            memory_type=MemoryType.SEMANTIC,
                            privacy=episodic.metadata.privacy,
                            categories={"extracted_fact"},
                            provenance=[str(episodic.id)],
                        ),
                    )
                )
            return facts
        except Exception:
            logger.exception("LLM semantic extraction failed; returning empty list")
            return []

    # --- Procedural: episodes → recurring patterns ---

    def extract_procedural(self, episodes: List[Memory]) -> List[Memory]:
        if len(episodes) < 2:
            return []
        try:
            joined = "\n\n---\n\n".join(ep.payload.raw for ep in episodes)
            raw = self._llm.chat(
                [
                    {"role": "system", "content": _PROCEDURAL_SYSTEM},
                    {"role": "user", "content": joined},
                ],
                max_tokens=512,
                temperature=0.0,
                timeout=self._timeout,
            ).strip()
            patterns = self._parse_json_array(raw)
            procs: List[Memory] = []
            for item in patterns:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("pattern", "")).strip()
                desc = str(item.get("description", "")).strip()
                freq = int(item.get("frequency", 0))
                if not name or freq < 2:
                    continue
                procs.append(
                    Memory(
                        id=MemoryId(),
                        anchor=TemporalAnchor(created_at=time.time()),
                        context=episodes[0].context,
                        payload=MemoryPayload(
                            raw=f"{name}: {desc} (observed {freq} times)",
                            structured={"pattern": name, "frequency": freq},
                        ),
                        metadata=MemoryMetadata(
                            memory_type=MemoryType.PROCEDURAL,
                            privacy=PrivacyLevel.PUBLIC,
                            categories={"pattern", "procedure"},
                        ),
                    )
                )
            return procs
        except Exception:
            logger.exception("LLM procedural extraction failed; returning empty list")
            return []

    # --- Helpers ---

    @staticmethod
    def _parse_json_array(text: str) -> List[Any]:
        """Parse JSON array from LLM output. Tolerant of ```json fences."""
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown code fence
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find first [...] in the text
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                data = json.loads(text[start : end + 1])
            else:
                raise
        if not isinstance(data, list):
            raise ValueError("LLM output is not a JSON array")
        return data
```

### 5.4 `src/uams/config.py` 新增字段(追加到 `UAMSConfig`)

```python
# --- LLM Compression ---
llm_enabled: bool = False
llm_provider: str = "openai_compatible"  # openai_compatible | null
llm_api_key: Optional[str] = None
llm_base_url: str = "https://api.openai.com/v1"
llm_model: str = "gpt-4o-mini"
llm_timeout_seconds: float = 30.0
llm_max_retries: int = 2
llm_max_tokens: int = 1024
llm_temperature: float = 0.0
llm_cache_enabled: bool = True
llm_cache_max_entries: int = 1000
llm_compression_max_events: int = 20
llm_compression_target_ratio: float = 0.3
```

### 5.5 `src/uams/config.py` `from_env()` 新增映射

```python
llm_enabled=cls._env_bool("UAMS_LLM_ENABLED", False),
llm_provider=cls._env_str("UAMS_LLM_PROVIDER", "openai_compatible"),
llm_api_key=os.getenv("UAMS_LLM_API_KEY", None),
llm_base_url=cls._env_str("UAMS_LLM_BASE_URL", "https://api.openai.com/v1"),
llm_model=cls._env_str("UAMS_LLM_MODEL", "gpt-4o-mini"),
llm_timeout_seconds=cls._env_float("UAMS_LLM_TIMEOUT", 30.0),
llm_max_retries=cls._env_int("UAMS_LLM_MAX_RETRIES", 2),
llm_max_tokens=cls._env_int("UAMS_LLM_MAX_TOKENS", 1024),
llm_temperature=cls._env_float("UAMS_LLM_TEMPERATURE", 0.0),
llm_cache_enabled=cls._env_bool("UAMS_LLM_CACHE", True),
llm_cache_max_entries=cls._env_int("UAMS_LLM_CACHE_MAX", 1000),
llm_compression_max_events=cls._env_int("UAMS_LLM_COMPRESS_MAX_EVENTS", 20),
llm_compression_target_ratio=cls._env_float("UAMS_LLM_TARGET_RATIO", 0.3),
```

### 5.6 `src/uams/config.py` `validate()` 新增校验

```python
# --- LLM Compression ---
if self.llm_provider not in ("openai_compatible", "null"):
    errors.append(f"llm_provider must be openai_compatible|null, got {self.llm_provider!r}")
if self.llm_enabled and not self.llm_api_key:
    errors.append("llm_api_key is required when llm_enabled=True")
if self.llm_timeout_seconds < 1 or self.llm_timeout_seconds > 300:
    errors.append("llm_timeout_seconds must be between 1 and 300")
if self.llm_max_tokens < 64 or self.llm_max_tokens > 8192:
    errors.append("llm_max_tokens must be between 64 and 8192")
if self.llm_temperature < 0.0 or self.llm_temperature > 2.0:
    errors.append("llm_temperature must be between 0.0 and 2.0")
if self.llm_cache_max_entries < 1:
    errors.append("llm_cache_max_entries must be >= 1")
if self.llm_compression_max_events < 1 or self.llm_compression_max_events > 200:
    errors.append("llm_compression_max_events must be between 1 and 200")
if self.llm_compression_target_ratio <= 0 or self.llm_compression_target_ratio > 1.0:
    errors.append("llm_compression_target_ratio must be in (0, 1.0]")
```

### 5.7 `src/uams/system.py` 改动(`__init__` 里 `HeuristicCompressionEngine` 那行附近)

```python
# 替换原 line 76:
#   self._compression = compression or HeuristicCompressionEngine()
# 为:

if compression is not None:
    self._compression = compression
elif self._config.llm_enabled and self._config.llm_api_key:
    try:
        from uams.llm.client import OpenAICompatibleClient, CachedLLMClient
        from uams.pipeline.llm_compression import LLMCompressionEngine

        inner = OpenAICompatibleClient(
            api_key=self._config.llm_api_key,
            base_url=self._config.llm_base_url,
            model=self._config.llm_model,
            timeout=self._config.llm_timeout_seconds,
            max_retries=self._config.llm_max_retries,
        )
        client = (
            CachedLLMClient(inner, max_entries=self._config.llm_cache_max_entries)
            if self._config.llm_cache_enabled
            else inner
        )
        self._compression = LLMCompressionEngine(
            client,
            max_events_per_call=self._config.llm_compression_max_events,
            target_ratio=self._config.llm_compression_target_ratio,
            timeout=self._config.llm_timeout_seconds,
        )
        logger.info("LLM compression engine enabled: provider=%s model=%s", self._config.llm_provider, self._config.llm_model)
    except Exception:
        logger.exception("Failed to initialize LLMCompressionEngine, falling back to HeuristicCompressionEngine")
        self._compression = HeuristicCompressionEngine()
else:
    self._compression = HeuristicCompressionEngine()
```

### 5.8 `pyproject.toml` 加 optional dep

```toml
[project.optional-dependencies]
# ... existing entries ...
llm = ["openai>=1.0"]
```

---

## 6. 测试骨架(`tests/test_llm_compression.py`)

```python
"""Tests for LLMCompressionEngine and LLM client implementations.

All tests use mock LLM clients — no real API calls, no token spend.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams import (
    AgentContext, AgentEvent, EventType, Memory, MemoryId, MemoryType,
    PrivacyLevel, TemporalAnchor,
)
from uams.llm.client import (
    CachedLLMClient, LLMClient, NullLLMClient, OpenAICompatibleClient,
)
from uams.pipeline.llm_compression import LLMCompressionEngine
from uams.config import UAMSConfig


class FakeLLMClient(LLMClient):
    """Records calls and returns scripted responses in order."""
    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = []

    def chat(self, messages, **kwargs):
        self._calls.append({"messages": messages, "kwargs": kwargs})
        if not self._responses:
            raise RuntimeError("FakeLLMClient: no scripted response left")
        return self._responses.pop(0)


def _make_event(content, agent_id="a1", session_id="s1", ts=1.0):
    return AgentEvent(
        event_type=EventType.USER_INPUT,
        agent_context=AgentContext(agent_id=agent_id, agent_type="t", session_id=session_id),
        content=content,
        timestamp=ts,
    )


class TestLLMClientCache(unittest.TestCase):
    def test_cache_returns_same_result(self):
        inner = FakeLLMClient(["cached-response"])
        cached = CachedLLMClient(inner, max_entries=10)
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(cached.chat(msgs), "cached-response")
        self.assertEqual(cached.chat(msgs), "cached-response")
        self.assertEqual(len(inner._calls), 1)

    def test_cache_evicts_when_full(self):
        inner = FakeLLMClient(["a", "b", "c", "d"])
        cached = CachedLLMClient(inner, max_entries=2)
        for i, content in enumerate(["m1", "m2", "m3"]):
            cached.chat([{"role": "user", "content": content}])
        self.assertEqual(len(inner._calls), 3)


class TestLLMCompressionEngine(unittest.TestCase):
    def _engine(self, client):
        return LLMCompressionEngine(client, max_events_per_call=5, timeout=5.0)

    def test_compress_episodic_calls_llm(self):
        client = FakeLLMClient(["Alice is vegetarian and likes boutique hotels."])
        engine = self._engine(client)
        events = [
            _make_event("I'm vegetarian", ts=1.0),
            _make_event("I prefer boutique hotels", ts=2.0),
        ]
        mem = engine.compress_working_to_episodic(events)
        self.assertEqual(mem.payload.raw, "Alice is vegetarian and likes boutique hotels.")
        self.assertEqual(mem.metadata.memory_type, MemoryType.EPISODIC)
        self.assertEqual(len(client._calls), 1)
        # System + user
        self.assertEqual(client._calls[0]["messages"][0]["role"], "system")

    def test_compress_fallback_on_llm_error(self):
        client = FakeLLMClient([])  # raises immediately
        engine = self._engine(client)
        events = [_make_event("hello", ts=1.0)]
        mem = engine.compress_working_to_episodic(events)
        # Falls back to raw concatenation
        self.assertIn("[USER_INPUT] hello", mem.payload.raw)

    def test_extract_semantic_parses_json(self):
        client = FakeLLMClient([
            json.dumps([{"key": "diet", "value": "vegetarian"},
                        {"key": "hotel_pref", "value": "boutique"}])
        ])
        engine = self._engine(client)
        episodic = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="a1", agent_type="t", session_id="s1"),
            payload=MemoryPayload(raw="Alice is vegetarian and likes boutique hotels."),
            metadata=__import__("uams").MemoryMetadata(
                memory_type=MemoryType.EPISODIC, privacy=PrivacyLevel.INTERNAL,
            ),
        )
        facts = engine.extract_semantic(episodic)
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0].payload.raw, "diet = vegetarian")

    def test_extract_semantic_handles_code_fence(self):
        client = FakeLLMClient(['```json\n[{"key": "k", "value": "v"}]\n```'])
        engine = self._engine(client)
        episodic = Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="a1", agent_type="t", session_id="s1"),
            payload=MemoryPayload(raw="something"),
            metadata=__import__("uams").MemoryMetadata(
                memory_type=MemoryType.EPISODIC, privacy=PrivacyLevel.INTERNAL,
            ),
        )
        facts = engine.extract_semantic(episodic)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].payload.raw, "k = v")

    def test_extract_procedural_requires_two_episodes(self):
        client = FakeLLMClient([])
        engine = self._engine(client)
        self.assertEqual(engine.extract_procedural([]), [])

    def test_extract_procedural_filters_low_frequency(self):
        client = FakeLLMClient([
            json.dumps([
                {"pattern": "p1", "description": "d", "frequency": 3},
                {"pattern": "p2", "description": "d", "frequency": 1},  # skip
            ])
        ])
        engine = self._engine(client)
        # Build 2 dummy episodes
        episodes = []
        for i in range(2):
            episodes.append(Memory(
                id=MemoryId(),
                anchor=TemporalAnchor(),
                context=AgentContext(agent_id="a1", agent_type="t", session_id="s1"),
                payload=MemoryPayload(raw=f"episode {i}"),
                metadata=__import__("uams").MemoryMetadata(
                    memory_type=MemoryType.EPISODIC, privacy=PrivacyLevel.INTERNAL,
                ),
            ))
        procs = engine.extract_procedural(episodes)
        self.assertEqual(len(procs), 1)
        self.assertIn("p1", procs[0].payload.raw)


class TestUAMSConfigLLMFields(unittest.TestCase):
    def test_default_llm_disabled(self):
        cfg = UAMSConfig()
        self.assertFalse(cfg.llm_enabled)
        self.assertIsNone(cfg.llm_api_key)

    def test_llm_enabled_requires_api_key(self):
        with self.assertRaises(ValueError):
            UAMSConfig(llm_enabled=True, llm_api_key=None).validate()

    def test_invalid_provider(self):
        with self.assertRaises(ValueError):
            UAMSConfig(llm_provider="anthropic", llm_api_key="dummy").validate()

    def test_invalid_target_ratio(self):
        with self.assertRaises(ValueError):
            UAMSConfig(llm_compression_target_ratio=0.0).validate()


if __name__ == "__main__":
    unittest.main()
```

---

## 7. 详细步骤(可复制 plan)

按顺序执行,每步可独立 commit。

### Step 1: 装 openai 依赖(本机开发)
```bash
pip install "openai>=1.0"
```

### Step 2: 创建 `src/uams/llm/` 模块
- 新建 `src/uams/llm/__init__.py`(粘 §5.2)
- 新建 `src/uams/llm/client.py`(粘 §5.1)

### Step 3: 写 `src/uams/pipeline/llm_compression.py`
- 新建,粘 §5.3

### Step 4: 改 `src/uams/config.py`
- 在 `UAMSConfig` dataclass 里追加 §5.4 的 13 个字段
- 在 `from_env()` 里追加 §5.5 的 13 个 env 映射
- 在 `validate()` 里追加 §5.6 的 7 个校验

### Step 5: 改 `src/uams/system.py`
- 替换 line 76 附近的 `HeuristicCompressionEngine()` 注入(粘 §5.7)
- 确保 import 在顶部加好(`from uams.llm.client import ...` 只在分支里 inline import,避免硬依赖)

### Step 6: 改 `pyproject.toml`
- 加 `llm = ["openai>=1.0"]` 到 `[project.optional-dependencies]`

### Step 7: 加测试
- 新建 `tests/test_llm_compression.py`(粘 §6)

### Step 8: 跑全部测试
```bash
python -m unittest discover -s tests
```
**期望**:`Ran 140+ tests in X.XXXs, OK (skipped=N)` — 132 原 + 9 新 + 一些字段相关测试全过。

### Step 9: 真实 LLM 端到端(可选,手工)
```bash
# 用 minimax 测(用户已有 key)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=<你的 minimax key>
export UAMS_LLM_BASE_URL=https://api.minimaxi.com/v1
export UAMS_LLM_MODEL=MiniMax-Text-01
python examples/personal_assistant.py
```
**期望**:会话结束时,Episodic memory 是自然语言 narrative(不是 `[TYPE] content\n...` 拼接),Semantic 是结构化 facts。

### Step 10: 真实压缩比测量(可选)
对比 50 个 events:
- `HeuristicCompressionEngine` 输出的 raw token 数
- `LLMCompressionEngine` 输出的 raw token 数
- **期望压缩比 ≥ 30%**(LLM 输出 ≤ heuristic 的 30%)

---

## 8. 验收标准(完成必须满足)

- [ ] 132 原有测试 + 9 新 LLM 测试全过
- [ ] 默认 `UAMSConfig()` → `HeuristicCompressionEngine`(向后兼容)
- [ ] `config.llm_enabled=True` + 有效 api_key → `LLMCompressionEngine`
- [ ] LLM 调用失败 → 自动 fallback 到 `Heuristic`,日志 WARNING,系统不挂
- [ ] Prompt 模板通过单元测试(mock)
- [ ] `pyproject.toml` 加 `openai>=1.0` 到 `[llm]` optional deps
- [ ] README 加 LLM Compression 配置示例(参考 §10 模板)
- [ ] (可选)真实 LLM 端到端跑通,压缩比 ≥ 30%

---

## 9. 回执模板

完成后回复:
```
PR1-2 完成
- 新增: src/uams/llm/{client.py,__init__.py}, src/uams/pipeline/llm_compression.py, tests/test_llm_compression.py
- 改动: src/uams/config.py, src/uams/system.py, pyproject.toml, README.md
- 测试: X 测试通过(M 个新增)
- 真实 LLM 跑通: [yes/no],压缩比 X%
- 已知问题: ...
- 建议下一步: ...
```

---

## 10. README 配置示例(完成后追加)

在 README 的 "## ⚙️ Configuration" 区域加:

```markdown
### LLM Compression (optional)

UAMS can use an LLM to compress session history into concise narratives
(typically 70%+ token reduction). Enable via environment:

\`\`\`bash
# OpenAI
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=sk-...
export UAMS_LLM_MODEL=gpt-4o-mini

# MiniMax
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=<minimax-key>
export UAMS_LLM_BASE_URL=https://api.minimaxi.com/v1
export UAMS_LLM_MODEL=MiniMax-Text-01

# Local ollama (OpenAI-compatible mode)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=ollama  # required but unused
export UAMS_LLM_BASE_URL=http://localhost:11434/v1
export UAMS_LLM_MODEL=llama3.1
\`\`\`

If the LLM call fails (network / quota / timeout), UAMS automatically falls
back to heuristic compression so the agent loop never stalls.
```

---

## 11. 关键技术点(容易踩的坑)

1. **OpenAI 兼容 ≠ OpenAI SDK**:`base_url` 改了就行,但有些 provider 不支持某些参数(例如 `temperature=0` 严格相等的语义)。先 `temperature=0.0` 试,不行再放开。
2. **Prompt 必须严格**:JSON 输出格式如果模型不稳,`_parse_json_array` 已经容错,但极端 case 要 fallback。
3. **Fallback 不能 raise**:`except Exception` 包住所有 LLM 调用,失败就退化到原始拼接。系统不能因 LLM 故障挂掉。
4. **缓存键必须包含 kwargs**:`max_tokens` / `temperature` 不同不能命中同一缓存。
5. **`__init__.py` 不要硬 import openai**:延迟到 `OpenAICompatibleClient` 里 import,避免缺依赖时整个 `uams` 模块 import 失败。
6. **Anthropic 不支持**:本次范围外,后续单独 PR 加 `AnthropicClient`。
7. **测试 mock**:不要真烧 token 跑测试。`FakeLLMClient` 返回固定字符串即可。

---

## 12. 已知风险

1. **Prompt 质量**:内置 prompt 对英文友好,中文/多语言效果未验证。如果用户多语言场景多,要后续优化 prompt。
2. **MiniMax 兼容**:`https://api.minimaxi.com/v1` 是 MiniMax 的 OpenAI 兼容端点(待用户验证)。如果参数差异大,需要加 provider-specific 配置。
3. **大 session OOM**:单 session 1000+ events 时,batch 摘要的"二级摘要"可能还是太长。当前 `max_events_per_call=20` 默认值,够用。
4. **缓存跨进程**:当前 `CachedLLMClient` 是进程内缓存。多实例部署需要 Redis 缓存(后续 PR2/P3 范围)。
5. **Anthropic prompt 格式不同**:Anthropic 用 `system` 字段在 messages 里,跟 OpenAI 不一样。本次只 OpenAI 兼容。

---

## 13. 范围外(本次不做,后续 PR)

- Anthropic SDK 独立支持
- Streaming 输出
- Prompt 版本管理 + rollback
- JSON schema 严格校验
- 多 LLM 投票
- 跨进程 Redis 缓存
- Prompt 多语言版本
- `prompt_overrides` 用户自定义(字段预留,接口未实装)