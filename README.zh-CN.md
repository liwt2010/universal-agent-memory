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
| **100k 并发压测（A+ 必备）** | `benchmarks/stress_test.py` 跑 100k 操作并发,JSON 报告上传 CI artifact ([docs/STRESS_TEST.md](docs/STRESS_TEST.md)) |
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
├── tests/                  # 375 个测试
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

**测试规模**:375 测试(本地 32 skip:无 PG/Redis/Neo4j service 时跳过真实后端;CI 上全部跑通)。

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

Apache-2.0

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
