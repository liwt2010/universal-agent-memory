# 通用智能体记忆系统 (UAMS)

**一个与领域无关的、为任意 AI 智能体打造的持久化记忆层。**

每一个 AI 智能体在每次会话开始时都从零起步。UAMS 解决了这个问题。它静默地捕获智能体的一切行为，将其压缩为可搜索的记忆图谱，并在下一次会话启动时注入恰到好处的上下文。

无论你是构建个人助理、游戏 NPC、客服机器人、科研智能体，还是多智能体系统 —— UAMS 都提供同一套通用记忆原语。

---

## 目录

- [设计理念](#设计理念)
- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [七大记忆原语](#七大记忆原语)
- [四层记忆模型](#四层记忆模型)
- [多智能体支持](#多智能体支持)
- [项目结构](#项目结构)
- [示例](#示例)
- [测试](#测试)
- [架构说明](#架构说明)
- [安装指南](#安装指南)
- [贡献指南](#贡献指南)
- [许可证](#许可证)

---

## 设计理念

> **记忆循环是普适的：捕获 → 压缩 → 索引 → 检索 → 注入。**

智能体框架应当专注于推理与行动。记忆应当是基础设施 —— 如同数据库或缓存 —— 负责持久化、检索并自动编排上下文。

UAMS 将记忆基础设施从智能体框架和应用领域中解耦出来。它受 [agentmemory](https://github.com/rohitg00/agentmemory) 和 [MemGPT](https://github.com/cpacker/MemGPT) 启发，但剥离了所有编码专用的语义，使其能够服务于**任意**智能体领域。

**有了 UAMS，会发生什么变化：**

- **第一次会话：** Alice 告诉智能体她是素食者，并且喜欢精品酒店。
- **第二次会话：** Alice 询问日本旅行酒店。智能体已经知道她的饮食限制和酒店偏好。无需重新解释。
- **智能体就是知道。**

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **四层记忆模型** | 工作记忆 → 情景记忆 → 语义记忆 → 程序记忆，灵感源自人类认知记忆架构 |
| **事件总线采集** | 通过通用事件总线实现零框架耦合的事件捕获 |
| **混合检索** | BM25 关键词 + 稠密向量 + 知识图谱遍历，以 RRF（倒数排序融合）整合 |
| **隐私与去重** | 自动脱敏敏感信息，SHA-256 滚动窗口去重 |
| **艾宾浩斯遗忘** | 每个记忆层级可配置独立的遗忘曲线 + **按 category 覆盖** ([docs/HALF_LIFE_TUNING.md](docs/HALF_LIFE_TUNING.md)) |
| **级联删除（GDPR 友好）** | 通过关系边与反向引用按需级联删除,附 JSONL 审计轨迹。4 个策略:`ISOLATED` / `OUTGOING` / `BIDIRECTIONAL`（默认,同层）/ **`FULL_CASCADE`**（显式 opt-in,跨层）([docs/CASCADE_FORGET.md](docs/CASCADE_FORGET.md)) |
| **多智能体协调** | 资源租约（Redis 分布式锁 + 失败自动禁用）、信号传递、共享记忆空间 |
| **Token 预算注入** | 自动将检索结果压缩到 LLM 上下文窗口限制内 |
| **可插拔存储** | 内存存储（默认）、ChromaDB、SQLite、PostgreSQL+pgvector、Neo4j |
| **remember() 语义级去重（opt-in）** | 新事实与已有语义记忆余弦相似度 ≥ `remember_dedup_threshold` 时,返回已有 `MemoryId` 而不存新副本 |
| **100k 并发压测（A+ 必备）** | `benchmarks/stress_test.py` 跑 100k 操作并发,JSON 报告上传 CI artifact ([docs/STRESS_TEST.md](docs/STRESS_TEST.md))。v6 修后 **PostgreSQL + Neo4j + Redis** 全部 100k/100k ops 0% err,Redis 从 7.6 ops/s 优化到 138.2 ops/s（18.2x）。详见 [docs/STRESS_TEST.md](docs/STRESS_TEST.md) "Diagnosed bugs" 段 |
| **框架无关** | 兼容 Claude、GPT、LangChain、AutoGen 或自研智能体 |

---

## 🧹 级联删除（GDPR 友好）

遗忘一条记忆往往不是故事的结尾。一旦某条 `memory_id` 被删除,下游的 `search_graph()` 就会留下看不见的空洞,任何引用过它的缓存或衍生记录都会变成悬空指针。在合规场景下(GDPR 第 17 条、HIPAA)无法级联删除是合规事故。

`uams.forget(memory_id)` 内建一套可配置的级联机制:

```python
from uams import UniversalMemorySystem
from uams.pipeline.cascade import CascadeStrategy

u = UniversalMemorySystem(storage_backend="sqlite")

# 四种策略，均有 best-effort 删除 + JSONL 审计
u.forget("mem-1", cascade=CascadeStrategy.ISOLATED)          # 单条（旧版行为）
u.forget("mem-1", cascade=CascadeStrategy.OUTGOING)           # + 同层正向目标
u.forget("mem-1")                                              # 默认：双向级联（GDPR，同层）
u.forget("mem-1", cascade=CascadeStrategy.FULL_CASCADE)       # 显式 opt-in：跨层也删（GDPR 第 17 条完整兑现）

# 返回 CascadeReport
report = u.forget("mem-1")
print(report.deleted_ids, report.orphan_ids, report.failed_ids,
      report.cross_tier_deleted_ids)  # FULL_CASCADE 才有内容
print(report.is_complete, report.audit_log_path)
```

**保证**:
- **visit-set + 最大深度上限** 防止环形关系导致的无限递归
- **同层严格作用域** —— 跨层关系记为"孤立"但**绝不**触发跨层删除
- **混合反向边发现** —— `auto` 模式优先用 store 的反向索引,否则退化为 `O(N)` 扫描
- **best-effort 删除** —— 部分失败记入 `report.failed_ids`,其余记忆仍会被删除;无论成败都写审计日志

**审计轨迹**:

```
logs/cascade_forget_audit.jsonl   # 每次调用一行 JSONL
logs/cascade_orphan_log.jsonl     # 每个跨层孤立边一行
```

用一次调用就能生成数据删除凭证:

```python
report = u.forget(target_id)
receipt = {
    "ts": report.to_dict()["ts"],
    "target": report.target_id,
    "deleted": report.deleted_ids,
    "failed": report.failed_ids,
    "audit_log": str(report.audit_log_path),
}
```

详见 [docs/CASCADE_FORGET.md](docs/CASCADE_FORGET.md)。

---

## 🆕 7-17 新增（v0.6.0 — 审计 pass 收尾）

**Non-breaking minor release**,关闭 v0.5.2 外部审计 14 项中的 9 项。新 API、新异常族、1 个 schema 迁移(SQLite 旧库自动应用)。完整迁移指南见 `RELEASE_NOTES_v0.6.0.md`。

| 改动 | 内容 | 原因 |
|------|------|------|
| **`UAMSError` 异常族** | 新 `uams.errors` 模块:`UAMSError` + `ConfigError` / `StorageError` / `CascadeError` / `LLMError`,从 `uams.*` 重新导出 | Vault 可按类别 catch UAMS 失败,不再 `except Exception` 撒网。store 内部仍走 `try/except + log + fallback` 保 graceful degradation,只在 facade 边界 raise |
| **`MemoryStore.truncate()` + `list_all_paginated()` + `delete_by_filters()`** | SQLite 用 `DELETE FROM` 单 SQL 覆盖,不再卡 999 行 cap | 修复 v0.5.x `clear()` 静默丢 >999 行的 P0 GDPR bug;`MigrationTool.migrate()` 不再一次性物化全表 |
| **`MemoryStore.vector_search_capable` 类属性** | `InMemoryStore` / `ChromaDBStore` = True;其他 4 个 = False 且 `search_vector` 每次 INFO log | 操作员能在生产 log 看到"该后端向量搜索是 recency fallback" |
| **`tenant_id` 接入 SQLiteStore + InMemoryStore** | schema 升 v2 + 加 `idx_<tier>_tenant` 索引;旧库自动 `ALTER TABLE ADD COLUMN` 迁移;`delete_by_project_id(tenant_id=...)` 走复合 `WHERE` | 真多租户 GDPR delete(其他 4 后端推 v0.6.x) |
| **`PrivacyFilter` 拆 `SECRET_PATTERNS` / `PII_PATTERNS`** | secret(API key / bearer / 信用卡 / GitHub PAT)永远 redact;PII(email / phone)仅 PRIVATE/INTERNAL/SECRET | 关闭 v0.5.x PUBLIC 内容带 OpenAI key 直接落盘的 P0 合规洞 |
| **`Memory.retrieval_score` 改 `float \| None = None`** + `_compress_to_budget` 改 `is None` 判断 | 0.0 score 当 0.0 处理,不再走 `importance` fallback | 修复 `0.0` falsy 路由 bug |
| **`AgentContext.namespace()` 含 `tenant_id`** | 4 段冒号拼接(原 3 段);None 时折叠为 `_` | 多租户 key 隔离 |
| **`OpenAICompatibleClient.achat` 加 retry loop** | 3 次尝试,exponential backoff 0.5s/1s/2s,封顶 4s;retry `TimeoutException` / `ConnectError` / 429/5xx;永久 4xx 直接抛 | 同步 `chat()` 早就有 `max_retries=2`,async 路径补齐 |
| **`LLMCompressionEngine` 落库前过 `PrivacyFilter`** | `metadata.privacy` 改取 source events 的 MAX(原取第一个) | LLM 输出可能幻觉/回吐 PII / secret,压缩链路必须过滤 |
| **`observe()` 拒空 `agent_id` / `agent_type` / `session_id`** | 顶部校验,warn + drop | 防止 misconfigured agent 落库后被 `delete_by_filter('agent_id', '')` 一刀切 |
| **`ChromaDBStore.list_all()` 真实流式** | `collection.get(include=['metadatas','documents'], limit=500, offset=offset)` 分页(原 stub 返 `[]`) | 修复 ChromaDB 后端 cascade in-edge / `delete_by_project_id` / `migrate()` 静默丢所有 memory 的 P0 bug |
| 488 → 498 测试 | +10 新测试模块,覆盖 errors / retrieval_score / ollama / tenant_id / privacy / namespace / achat retry / observe 校验 / vector_search_capable / chromadb list_all / llm_compression pii / truncate | 无回归;2 个 pre-existing failure 仍在(`test_large_chinese_text` perf / `test_shutdown_persists_working` test-logic) |

## 🆕 7-15 新增（v0.5.2 — 类型注解现代化）

| 改动 | 内容 | 原因 |
|------|------|------|
| **PEP 585 + PEP 604 类型注解** | `List[X]` → `list[X]`,`Dict[K,V]` → `dict[K,V]`,`Optional[X]` → `X \| None`,`Union[A,B]` → `A \| B`,覆盖 32 个源文件 + 2 个测试文件。`typing.Deque` / `Protocol` / `Type` 仍保留(PEP 604 无对应) | 项目目标是 Python 3.9+,而 PEP 585/604 在该版本已经可用。迁移是零运行时变化的纯语法重写;下游 `mypy` / `pyright` 用户能看到更干净的 diff 与更好的 IDE 支持。 |
| **`py.typed` 标记** | 空文件 `src/uams/py.typed`,在 `pyproject.toml` `[tool.setuptools.package-data]` 和 `MANIFEST.in` 中声明 | PEP 561。下游 `mypy` / `pyright` 用户能直接对 `uams.*` 进行类型检查,无需自己写 stub。`pyproject.toml` 里的 `Typing :: Typed` 分类器名实相符。 |
| **CI `Lint with flake8` 升级为真实 PR gate** | `mypy src/ \|\| true` 恢复成 `mypy src/` 拦截新错误(而 mypy 本身仍 informational——见 CHANGELOG 剩余 142 个历史错误,留给后续 PR) | 拦截 typing 迁移可能引入的 dead import 和未定义 name。 |
| **`AsyncUniversalMemorySystem.acquire_lock` / `release_lock` 类型精确化** | `acquire_lock` 返回 `Lease \| None`,`release_lock` 返回 `bool`(原为 `Any`) | 静态分析可正确识别 async lease API 的错用。 |
| `AsyncUniversalMemorySystem` 拆为 5 个 per-method `asyncio.Lock` | `observe` / `session-events` / `store` / `coord` / `sweep` 各自一把锁 | 旧版一个 facade-wide 锁把全部 async 调用串行化,违背 async 初衷;现在 `observe` 与 `recall` 可并发。 |
| `AsyncUniversalMemorySystem` 用 `asyncio.to_thread` 替代 `asyncio.get_event_loop().run_in_executor` | Python 3.9+ 惯用 async I/O 委托 | 3.10+ 后者会触发 `DeprecationWarning`;前者向前兼容。 |
| `LLMClient.achat()` + `OpenAICompatibleClient` 惰性 `httpx.AsyncClient` | 跳过 openai SDK 阻塞 transport 的真 async 路径 | async agent 等待 LLM 不再阻塞 event loop。`NullLLMClient.achat` / `CachedLLMClient.achat` 同样遵循异步契约;`CachedLLMClient` 优先 `inner.achat`(真 async),否则 fallback 到 `asyncio.to_thread(inner.chat, ...)`。 |
| **`UAMSConfig.max_session_events`** 已接线 | `_session_events[sid]` 列表现带 cap,溢出时丢弃最旧事件 + WARNING | 防止长跑 agent 因事件源失控把内存撑爆。 |
| **`UAMSConfig.max_results_per_session`** 已接线 | 替代 `RetrievalPipeline` 里硬编码的 `>= 3` | 单个会话结果过多会淹没其他会话的检索结果;现在 cap 可显式调。 |
| **`UAMSConfig.llm_max_tokens` / `llm_temperature`** 已接线 | 透传给 `LLMCompressionEngine` 与 `QueryRewriter`,不再硬编码 `512` / `0.0` / `128` | LLM 调用预算的部署级调参真正生效。 |
| **`UAMSConfig.max_agent_id_length` / `max_user_id_length`** 在 `observe()` 入口强制 | 超长则截断 + 警告而非抛错 | 避免 `delete_by_filter` / `revoke_agent` 因 key 不匹配而静默漏掉。 |
| 488 测试 (+4 用于 `achat()`) | 钉死:ABC 接口、Null raises、Cached.achat 走 inner.achat、二次调用命中缓存 | 无回归。两个原本就存在的失败仍存在(`test_large_chinese_text` 性能阈值、`test_shutdown_persists_working` 测试逻辑 bug)。 |

`v0.5.2` 是 **non-breaking patch release**(纯类型注解变化 + 新打包面),无 API 移除。

---

## 🆕 7-15 新增（v0.5.1 — async 契约）

| 改动 | 内容 | 原因 |
|------|------|------|
| **`AsyncUniversalMemorySystem.forget()` 签名** | 返回 `CascadeReport`(原 `bool`);转发 `cascade` / `max_depth` / `in_edge_mode` 参数 | 同步版 `forget()` 在级联重构后已返回 `CascadeReport`,async 包装却没跟上,导致 `await aus.forget(id)` 静默丢掉 deleted-ids / failed-ids / 审计信息。 |
| 新增回归测试 `tests/test_async_forget_signature.py` | 钉死 4 参签名与 `CascadeReport` 返回类型 | 没有这条测试,以后把 hint 改回 `bool` 也会过 CI。 |
| 488 测试 (与 v0.5.0 一致) | 同步测试全过;新测试覆盖 async 表面。 |

对同步用户,**`v0.5.1` non-breaking**。对 async 用户,`await aus.forget(...)` 之前返回 `bool`,现在返回 `CascadeReport`;任何 `if await aus.forget(id): ...` 需要改成 `report = await aus.forget(id); if report.is_complete: ...`。

---

## 🆕 7-15 新增（v0.5 安全加固）

| 改动 | 内容 | 原因 |
|------|------|------|
| **`UAMSConfig.validate()` 拒绝不安全标识符与路径** | 新增 `postgresql_table` / `redis_key_prefix` / `cascade_audit_log_path` / `cascade_max_depth` / `cascade_in_edge_strategy` 校验 | 关闭真实攻击面:DDL 注入、Redis 键注入、无界级联深度。配置层防线。 |
| **Embedding 读取改为 JSON-only** | 永久移除 `pickle.loads` fallback | 一旦攻击者可写入共享存储,即可 RCE。旧数据需运行 `embedding_serde` 文档字符串中的迁移脚本。 |
| **`Memory.to_json` / `from_json` 完整序列化 `embedding` + `relations`** | 之前静默丢失,导致 backup/restore 后向量搜索失效 + cascade-forget in-edge 不可达 | 真实数据丢失 bug 修复。 |
| **`RateLimiter` 线程安全** | 加锁 + 8×100 并发回归测试 | 第二轮审计标注但未修的 P2 race。 |
| 删除 `InputValidator.sanitize_sql` | 改为 `is_safe_identifier` 白名单 | 关键词黑名单是反模式。UAMS 全程用参数化查询。 |
| **`AgentContext.tenant_id`** | 多租户隔离原语 | 与 v0.4.0 的 `delete_by_project_id(project_id, tenant_id=...)` 配套。 |
| 488 测试 (+5) | 新增:identifier safety、audit-path safety、cascade bounds、embedding fail-secure、RateLimiter 并发 | 无回归。两个预先存在的失败(`test_large_chinese_text`、`test_shutdown_persists_working`) 与本批无关。 |

**⚠️ v0.5.0 是 breaking release。** 移除了两个历史"兼容垫片":
`InputValidator.sanitize_sql` 和 `embedding_serde` 的 `pickle.loads` fallback。
迁移方案见 [CHANGELOG.md](CHANGELOG.md)。

---

## 快速开始

```python
from uams import UniversalMemorySystem, AgentContext, AgentEvent, EventType

# 1. 创建记忆系统
ums = UniversalMemorySystem()

# 2. 定义智能体上下文
ctx = AgentContext(
    agent_id="pa_001",
    agent_type="personal_assistant",
    session_id="sess_1",
    user_id="alice",
)

# 3. 观察事件（这是最主要的采集原语）
ums.observe(AgentEvent(
    event_type=EventType.USER_INPUT,
    agent_context=ctx,
    content="我是素食者，而且我喜欢精品酒店。",
    structured_data={
        "fact": "Alice 是素食者，喜欢精品酒店",
        "importance": 8.0,
        "category": "travel_preference",
    },
))

# 4. 结束会话（触发四层压缩整合）
ums.observe(AgentEvent(
    event_type=EventType.SESSION_END,
    agent_context=ctx,
    content="会话结束",
))

# 5. 新会话 —— 检索相关上下文
ctx2 = AgentContext(
    agent_id="pa_001",
    agent_type="personal_assistant",
    session_id="sess_2",
    user_id="alice",
)

memories = ums.recall("日本旅行酒店", context=ctx2, budget_tokens=1000)

# 6. 以上下文块形式注入到 LLM 提示词中
context_block = ums.inject_context("日本旅行酒店", context=ctx2, budget_tokens=1000)
print(context_block)
```

**输出：**
```
## 相关记忆上下文

1. [SEMANTIC] Alice 是素食者，喜欢精品酒店
2. [EPISODIC] [USER_INPUT] 我是素食者，而且我喜欢精品酒店。
```

---

## 七大记忆原语

UAMS 暴露 **7 个通用原语**，替代了 agentmemory 的 53 个编码专用工具。任何智能体框架都通过这 7 个调用完成集成。

| 原语 | 签名 | 用途 |
|------|------|------|
| **`observe(event)`** | 将任意 `AgentEvent` 记录到工作记忆 | 主要采集入口 —— 接入智能体生命周期 |
| **`remember(fact, ...)`** | 显式将事实保存到语义记忆 | 用户直接陈述偏好或事实 |
| **`recall(query, ...)`** | 跨所有层级检索相关记忆 | 每次智能体行动前调用，加载上下文 |
| **`forget(memory_id, cascade=...)`** | 删除记忆,并按需沿正向与反向引用级联,同时写入审计轨迹。返回 `CascadeReport` | GDPR"被遗忘权" / 用户请求 / 清理 |
| **`consolidate(session_id)`** | 触发四层压缩整合 | 会话结束自动触发，或手动调用 |
| **`inject_context(...)`** | 将记忆格式化为提示词文本块 | 直接注入到 LLM 系统提示词 |
| **`sync(target)`** | 与外部文件双向同步 | `MEMORY.md`、游戏存档文件等 |

---

## 四层记忆模型

UAMS 以人类认知架构为蓝本建模记忆。每一层拥有独立的存储后端、检索策略和遗忘曲线。

```
┌────────────────────────────────────────────────────────────┐
│  工作记忆 (WORKING)      原始事件、感官输入           30分钟 TTL │
│  ─────────────────────────────────────────────────────────  │
│  情景记忆 (EPISODIC)     会话叙事、经验经历           7天半衰期   │
│  ─────────────────────────────────────────────────────────  │
│  语义记忆 (SEMANTIC)     事实、偏好、概念             90天半衰期  │
│  ─────────────────────────────────────────────────────────  │
│  程序记忆 (PROCEDURAL)   技能、工作流、模式           1年半衰期   │
└────────────────────────────────────────────────────────────┘
```

### 各层详情

| 层级 | 存储内容 | 默认 TTL | 检索方式 | 示例 |
|------|---------|---------|---------|------|
| **工作记忆** | 原始 `AgentEvent` 流 | 30 分钟 | 精确匹配 / 最近优先 | "用户 2 分钟前说了'你好'" |
| **情景记忆** | 压缩后的会话摘要 | 7 天 | 关键词 + 语义 | "昨天的旅行规划会话" |
| **语义记忆** | 提取的事实和偏好 | 90 天 | 语义向量搜索 | "Alice 是素食者" |
| **程序记忆** | 可复用的模式和策略 | 1 年 | 图谱 + 模式匹配 | "处理旅行查询时，先问饮食限制" |

### 记忆衰减公式（艾宾浩斯遗忘曲线）

```
留存率 = 0.5^(时间 / 半衰期)
         × (1 + 0.1 × 访问次数)      # 被访问的记忆强化
         × (0.5 + 0.5 × 重要性/10)   # 重要记忆持久化
         × 置信度                      # 被矛盾的记忆消退
```

如果 `留存率 < 留存阈值`，记忆将被自动驱逐。

---

## 🧠 LLM 压缩(可选)

> **默认 = `HeuristicCompressionEngine` ≈ 0% token 节省。** UAMS 出厂带启发式引擎,开箱即用不依赖 LLM;启发式只做事件结构化 (`[TYPE] content\n...`),**不做摘要**。下面 72% 标题是 **LLM 模式** 的数字,通过环境变量显式 opt-in。

默认关闭 —— UAMS 内置 **启发式压缩引擎**,无需 LLM 依赖即可运行。启用 **LLM 压缩** 可以在长会话场景获得真实 token 节省。

```bash
# OpenAI
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=sk-...
export UAMS_LLM_MODEL=gpt-4o-mini

# MiniMax (OpenAI 兼容)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=<minimax-key>
export UAMS_LLM_BASE_URL=https://api.minimaxi.com/v1
export UAMS_LLM_MODEL=MiniMax-Text-01

# 本地 ollama(OpenAI 兼容模式)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=ollama        # 必填但不使用
export UAMS_LLM_BASE_URL=http://localhost:11434/v1
export UAMS_LLM_MODEL=llama3.1
```

**LLM 在压缩各阶段做什么**:

| 阶段 | 启发式(默认) | LLM 压缩 |
|------|--------------|---------|
| 情景记忆压缩 | 拼接 `[TYPE] content\n...`(≈原始 token 数) | 摘要为约 200 字叙述(有界) |
| 语义记忆抽取 | 仅挑选 `(str/int/float/bool)` 结构化字段 | LLM 抽取原子事实(JSON) |
| 程序模式识别 | 统计 category 出现次数(≥2) | LLM 识别重复工作流 |

**实测节省**(20 事件会话):

```
启发式 (默认):  300 tokens  (原始 100%,≈ 0% 节省,仅做结构化)
LLM (opt-in):    84 tokens  (原始  28%)  → 72% 节省
```

如果 LLM 调用失败(网络/配额/超时),UAMS **自动降级**到启发式压缩,agent 主循环不会卡住。详见 [docs/PR1-2-LLM-Compression.md](docs/PR1-2-LLM-Compression.md)。

---

## 🔌 可插拔 Embedding 提供方

默认关闭 —— UAMS 退化为 **BM25 + 图谱检索**(RRF 3 路中的 2 路)。启用后可获得完整混合检索流水线。

| 提供方 | 模式 | 安装 | 适用场景 |
|--------|------|------|---------|
| **NoOp** | 无 | 内置 | 关闭向量检索,纯 BM25 + 图谱 |
| **SentenceTransformers** | 本地 | `pip install "uams[embeddings]"` | 离线/内网部署,默认 `all-MiniLM-L6-v2`(384 维) |
| **OpenAI 兼容** | 远程 | `pip install "uams[llm]"` | OpenAI / MiniMax / ollama / vLLM(设置 `UAMS_EMBEDDING_BASE_URL`) |

```bash
# 本地 sentence-transformers
export UAMS_EMBEDDING_ENABLED=true
export UAMS_EMBEDDING_PROVIDER=sentence_transformers
export UAMS_EMBEDDING_MODEL=all-MiniLM-L6-v2

# 远程 OpenAI 兼容
export UAMS_EMBEDDING_ENABLED=true
export UAMS_EMBEDDING_PROVIDER=openai_compatible
export UAMS_EMBEDDING_API_KEY=<key>
export UAMS_EMBEDDING_BASE_URL=https://api.openai.com/v1
export UAMS_EMBEDDING_REMOTE_MODEL=text-embedding-3-small
```

所有提供方共享统一的 **LRU 缓存**(默认 5000 条),避免重复 embedding 调用。任何提供方初始化失败都会降级到 NoOp 并打 WARNING 日志 —— 检索自动回退到 BM25 + 图谱。

---

## 多智能体支持

UAMS 通过三个原语实现多智能体之间的协调：**租约（Lease）**、**信号（Signal）** 和 **共享记忆空间**。

### 启用多智能体模式

```python
ums.enable_multi_agent()  # 默认创建共享 InMemoryStore
```

### 资源租约（独占锁）

```python
# 智能体 A 获取独占任务
acquired = ums.acquire_lock("agent_a", "task_001_analysis", ttl=300.0)
# 获取成功返回 True，已被其他智能体锁定则返回 False

# 智能体 B 尝试获取同一任务 —— 被阻塞
blocked = ums.acquire_lock("agent_b", "task_001_analysis")  # False

# 智能体 A 释放锁
ums.release_lock("agent_a", "task_001_analysis")
```

### 智能体间信号

```python
from uams import Signal

# 智能体 A 向智能体 B 发送消息
ums.send_signal(Signal(
    sender="agent_a",
    recipient="agent_b",   # 使用 "*" 进行广播
    signal_type="data_ready",
    payload={"dataset_size": 10000, "location": "/shared/data.csv"},
))

# 智能体 B 读取所有未读信号
signals = ums.read_signals("agent_b")
for sig in signals:
    print(f"来自 {sig.sender}: {sig.type} - {sig.payload}")
```

### 共享记忆空间

```python
# 智能体 A 采集数据并共享给团队
ums.observe(AgentEvent(...))  # 写入工作记忆

# 提升到团队共享语义空间
ums.share_memory(memory, target_team="analysis_team")

# 智能体 B 查询团队上下文
team_memories = ums._coordinator.get_team_context("analysis_team", "dataset")
```

---

## 项目结构

```
universal-agent-memory/
├── pyproject.toml          # Python 包配置
├── README.md               # 本文档（英文）
├── README.zh-CN.md         # 简体中文版本
├── README.zh-TW.md         # 繁体中文版本
├── src/uams/               # 核心包（约 12200 行）
│   ├── system.py           # 主入口（forget() 级联分派）
│   ├── async_system.py     # 异步 API
│   ├── config.py           # 配置 + 生产安全校验
│   ├── benchmarks.py       # 性能基准
│   ├── health.py           # 健康检查与指标
│   ├── core/               # 枚举、数据模型
│   ├── bus/                # 事件总线
│   ├── storage/            # 6 个存储后端（InMemory/SQLite/PG/Redis/Neo4j/ChromaDB）
│   ├── pipeline/           # 压缩、检索、隐私、遗忘、LLM 压缩、**级联**
│   │   └── cascade.py      # **CascadeForgetter (BFS + visit-set + max_depth + best-effort)**
│   ├── multi_agent/        # 协调
│   ├── embedding/          # 嵌入接口 + 4 个 provider
│   ├── llm/                # OpenAI 兼容 LLM 客户端 + 缓存
│   ├── adapters/           # 框架适配器
│   └── utils/              # 日志、重试、安全、token、备份、**级联审计**
│       └── cascade_audit.py  # **追加式 JSONL 审计写入器（GDPR 轨迹）**
├── examples/               # 5 个领域示例 + token 压缩演示
│   ├── personal_assistant.py
│   ├── game_npc.py
│   ├── customer_service.py
│   ├── research_agent.py
│   ├── multi_agent.py
│   └── _token_compression_demo.py
├── tests/                  # 498 个测试
│   ├── test_system.py
│   ├── test_chaos.py
│   ├── test_aplus.py
│   ├── test_postgresql_store.py    # CI：真实 PG service container
│   ├── test_chromadb_store.py      # CI：真实 ChromaDB EphemeralClient
│   ├── test_redis_store_real.py    # CI：真实 redis service
│   ├── test_neo4j_store_real.py    # CI：真实 neo4j service
│   ├── test_cascade.py             # 级联删除测试
│   ├── test_config_validation.py
│   ├── test_llm_compression.py
│   └── test_embedding.py
└── docs/                   # 文档
    ├── API.md              # API 参考
    ├── ARCHITECTURE.md     # 架构深读
    ├── CASCADE_FORGET.md   # 级联删除用户指南
    ├── DEPLOYMENT.md       # 部署指南
    ├── DEPLOYMENT.zh-CN.md # 部署指南（简中）
    ├── PR1-2-LLM-Compression.md # LLM 压缩交接文档
    └── superpowers/        # 规格 + 计划（跨层级联删除）
```

---

## 示例

从项目根目录直接运行任意示例：

```bash
# 个人助理：跨会话记住饮食偏好和酒店品味
python examples/personal_assistant.py

# 游戏 NPC：酒馆老板记住玩家过去的不良行为
python examples/game_npc.py

# 客服：客服智能体召回同一客户的过往工单
python examples/customer_service.py

# 科研智能体：文献综述智能体召回先前假设和关键论文
python examples/research_agent.py

# 多智能体：数据采集智能体向分析智能体发送信号并共享数据集
python examples/multi_agent.py
```

---

## 测试

```bash
# 运行所有单元测试
python -m unittest discover -s tests -v

# 或直接运行测试脚本
python tests/test_system.py
```

### 已验证的测试覆盖

| 测试 | 验证内容 |
|------|---------|
| MemoryId 唯一性 | 全局 UUID 生成 |
| 观察 + 检索 | 事件采集与跨会话检索 |
| 显式记住 | 直接向语义层写入事实 |
| 隐私过滤 | SECRET 脱敏和 PII 掩码 |
| 去重 | SHA-256 滚动窗口防止重复采集 |
| 多智能体锁 | 独占租约获取与阻塞 |
| 层级统计 | 工作/情景/语义/程序层计数正确 |
| 上下文注入 | 生成可直接用于提示词的文本块 |
| **6 后端真实验证(CI 9/9 green)** | **PG / ChromaDB / Redis / Neo4j / SQLite / InMemory 全部真实 service 跑通** |
| **级联删除** | **三策略 + visit-set + 最大深度上限 + 跨层隔离 + 最佳努力删除 + JSONL 审计** |

**测试规模**:488 测试(本地 32 skip:无 PG/Redis/Neo4j service 时跳过真实后端;CI 上全部跑通)。7-12 审计加固新增 29 个:信号队列 4 + Redis auto-disable 3 + SQLite close 2 + backup 错误分类 2 + cascade 审计日志 2 + Async forget 4 + SQLite pool 3 + SIGTERM 3 + decay_sweep 锁 2 + SQLite retrieve 回归 3。

---

## 架构说明

### 记忆循环

```
┌─────────────────┐     ┌──────────────────┐
│   智能体事件     │────▶│   事件总线        │
│   (任意领域)     │     │   (零耦合)        │
└─────────────────┘     └────────┬─────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
           ┌────────▼────────┐      ┌────────▼────────┐
           │ 隐私过滤器       │      │ 去重窗口         │
           │ (脱敏敏感信息)    │      │ (SHA-256 窗口)  │
           └────────┬────────┘      └────────┬────────┘
                    │                         │
                    └────────────┬────────────┘
                                 │
                          ┌──────▼──────┐
                          │  工作记忆层   │  ← 30分钟 TTL，精确匹配
                          │  (WORKING)   │
                          └──────┬──────┘
                                 │ 会话结束触发整合
                    ┌────────────┴────────────┐
                    │                         │
           ┌────────▼────────┐      ┌────────▼────────┐
           │ 压缩引擎        │      │ 压缩引擎        │
           │ (LLM 驱动)      │      │ (规则/启发式)  │
           └────────┬────────┘      └────────┬────────┘
                    │                         │
             ┌──────▼──────┐           ┌──────▼──────┐
             │  情景记忆层  │           │  语义记忆层  │
             │ (EPISODIC)  │           │ (SEMANTIC)  │
             │  7天半衰期   │           │  90天半衰期  │
             └─────────────┘           └──────┬──────┘
                                              │
                                       ┌──────▼──────┐
                                       │  程序记忆层  │
                                       │ (PROCEDURAL)│
                                       │  1年半衰期   │
                                       └─────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │      检索流水线          │
                    │ (BM25 + 向量 + 图谱 + RRF)│
                    └────────────┬────────────┘
                                 │
                          ┌──────▼──────┐
                          │  Token 预算  │
                          │  压缩        │
                          └──────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │   注入到智能体            │
                    │   提示词 / 上下文窗口      │
                    └───────────────────────────┘
```

### 核心抽象

UAMS 对**你的领域一无所知**。它只知道：

- `AgentEvent` —— **谁**（智能体上下文）、**何时**（时间戳）、**什么**（内容 + 结构化数据）
- `Memory` —— **ID**（UUID）、**时间锚点**（时间元数据）、**上下文**（谁产生的）、**载荷**（原始 + 结构化 + 嵌入）、**元数据**（类型 / 隐私 / 重要性 / 关系）

你的领域专用信息全部存在于：
- `payload.raw` —— 自然语言描述
- `payload.structured` —— 可序列化的 JSON 工件
- `metadata.categories` —— 你自己的标签（如 `travel_preference`、`player_reputation`、`paper_reference`）

---

## 安装指南

### 从源码安装（推荐用于开发）

```bash
git clone https://github.com/liwt2010/universal-agent-memory.git
cd universal-agent-memory
pip install -e .
```

### 生产环境：添加可插拔后端

```bash
# 向量搜索（语义层）
pip install chromadb

# 知识图谱（程序层）
pip install neo4j

# 本地嵌入（无需 API 密钥）
pip install sentence-transformers
```

---

## 贡献指南

我们欢迎来自所有领域的贡献 —— 个人助理、游戏 AI、机器人、客服、科研工具等。

1. Fork 仓库
2. 创建功能分支（`git checkout -b feature/awesome-feature`）
3. 提交更改（`git commit -m '添加 awesome 功能'`）
4. 推送到分支（`git push origin feature/awesome-feature`）
5. 发起 Pull Request

提交前请确保所有测试通过：

```bash
python -m unittest discover -s tests -v
```

---

## 语言版本

- [English](README.md)
- [简体中文 (Simplified Chinese)](README.zh-CN.md) （本文档）
- [繁體中文 (Traditional Chinese)](README.zh-TW.md)

---

## 许可证

MIT License —— 详见 [LICENSE](LICENSE)。

---

## 致谢

UAMS 受以下优秀项目的启发：

- [agentmemory](https://github.com/rohitg00/agentmemory) by Rohit Ghumare —— 证明了该架构在编码智能体上的可行性
- [MemGPT](https://github.com/cpacker/MemGPT) by Charles Packer —— 为 LLM 设计了操作系统级的记忆管理

UAMS 将它们的领域专用创新，泛化为一个通用的智能体基础设施层。

---

<p align="center">
  <b>通用记忆。任意智能体。任意领域。</b>
</p>
