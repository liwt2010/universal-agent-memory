# 通用智能體記憶系統 (UAMS)

**一個與領域無關的、為任意 AI 智能體打造的持久化記憶層。**

每一個 AI 智能體在每次會話開始時都從零起步。UAMS 解決了這個問題。它靜默地擷取智能體的一切行為，將其壓縮為可搜尋的記憶圖譜，並在下一次會話啟動時注入恰到好處的上下文。

無論你是建構個人助理、遊戲 NPC、客服機器人、科研智能體，還是多智能體系統 —— UAMS 都提供同一套通用記憶原語。

---

## 目錄

- [設計理念](#設計理念)
- [核心特性](#核心特性)
- [快速開始](#快速開始)
- [七大記憶原語](#七大記憶原語)
- [四層記憶模型](#四層記憶模型)
- [多智能體支援](#多智能體支援)
- [專案結構](#專案結構)
- [範例](#範例)
- [測試](#測試)
- [架構說明](#架構說明)
- [安裝指南](#安裝指南)
- [貢獻指南](#貢獻指南)
- [授權條款](#授權條款)

---

## 設計理念

> **記憶迴路是普適的：擷取 → 壓縮 → 索引 → 檢索 → 注入。**

智能體框架應當專注於推理與行動。記憶應當是基礎設施 —— 如同資料庫或快取 —— 負責持久化、檢索並自動編排上下文。

UAMS 將記憶基礎設施從智能體框架和應用領域中解耦出來。它受 [agentmemory](https://github.com/rohitg00/agentmemory) 和 [MemGPT](https://github.com/cpacker/MemGPT) 啟發，但剝離了所有編碼專用的語義，使其能夠服務於**任意**智能體領域。

**有了 UAMS，會發生什麼變化：**

- **第一次會話：** Alice 告訴智能體她是素食者，而且喜歡精品旅館。
- **第二次會話：** Alice 詢問日本旅行旅館。智能體已經知道她的飲食限制和旅館偏好。無需重新解釋。
- **智能體就是知道。**

---

## 核心特性

| 特性 | 說明 |
|------|------|
| **四層記憶模型** | 工作記憶 → 情境記憶 → 語意記憶 → 程序記憶，靈感源自人類認知記憶架構 |
| **事件匯流排擷取** | 透過通用事件匯流排實現零框架耦合的事件擷取 |
| **混合檢索** | BM25 關鍵字 + 稠密向量 + 知識圖譜遍歷，以 RRF（倒數排序融合）整合 |
| **隱私與去重** | 自動脫敏敏感資訊，SHA-256 滾動視窗去重 |
| **艾賓浩斯遺忘** | 每個記憶層級可設定獨立的遺忘曲線 |
| **級聯刪除（GDPR 友善）** | 可沿關係邊與反向引用按需求級聯刪除,並附 JSONL 稽核軌跡([docs/CASCADE_FORGET.md](docs/CASCADE_FORGET.md)) |
| **多智能體協調** | 資源租約（Lease）、訊號傳遞（Signal）、共享記憶空間 |
| **Token 預算注入** | 自動將檢索結果壓縮到 LLM 上下文視窗限制內 |
| **可插拔儲存** | 記憶體儲存（預設）、ChromaDB、SQLite、PostgreSQL+pgvector、Neo4j |
| **框架無關** | 相容 Claude、GPT、LangChain、AutoGen 或自研智能體 |

---

## 🧹 級聯刪除（GDPR 友善）

遺忘一條記憶往往不是故事的結尾。一旦某筆 `memory_id` 被刪除,下游的 `search_graph()` 就會留下看不見的漏洞,任何引用過它的快取或衍生紀錄都會變成懸空指標。在合規場景下(GDPR 第 17 條、HIPAA)無法級聯刪除即為合規事件。

`uams.forget(memory_id)` 內建一套可設定的級聯機制:

```python
from uams import UniversalMemorySystem
from uams.pipeline.cascade import CascadeStrategy

u = UniversalMemorySystem(storage_backend="sqlite")

# 三種策略,皆有 best-effort 刪除 + JSONL 稽核
u.forget("mem-1", cascade=CascadeStrategy.ISOLATED)        # 單筆(舊版行為)
u.forget("mem-1", cascade=CascadeStrategy.OUTGOING)         # + 同層正向目標
u.forget("mem-1")                                            # 預設:雙向級聯(GDPR)

# 回傳 CascadeReport
report = u.forget("mem-1")
print(report.deleted_ids, report.orphan_ids, report.failed_ids)
print(report.is_complete, report.audit_log_path)
```

**保證**:
- **visit-set + 最大深度上限** 防止環狀關係導致的無限遞迴
- **同層嚴格作用域** —— 跨層關係記為「孤立」但**絕不**觸發跨層刪除
- **混合反向邊探索** —— `auto` 模式優先用 store 的反向索引,否則退回為 `O(N)` 掃描
- **best-effort 刪除** —— 部分失敗寫入 `report.failed_ids`,其餘記憶仍會被刪除;無論成敗皆寫稽核日誌

**稽核軌跡**:

```
logs/cascade_forget_audit.jsonl   # 每次呼叫一行 JSONL
logs/cascade_orphan_log.jsonl     # 每個跨層孤立邊一行
```

用一次呼叫即可產生資料刪除憑證:

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

詳見 [docs/CASCADE_FORGET.md](docs/CASCADE_FORGET.md)。

---

## 快速開始

```python
from uams import UniversalMemorySystem, AgentContext, AgentEvent, EventType

# 1. 建立記憶系統
ums = UniversalMemorySystem()

# 2. 定義智能體上下文
ctx = AgentContext(
    agent_id="pa_001",
    agent_type="personal_assistant",
    session_id="sess_1",
    user_id="alice",
)

# 3. 觀察事件（這是最主要的擷取原語）
ums.observe(AgentEvent(
    event_type=EventType.USER_INPUT,
    agent_context=ctx,
    content="我是素食者，而且我喜歡精品旅館。",
    structured_data={
        "fact": "Alice 是素食者，喜歡精品旅館",
        "importance": 8.0,
        "category": "travel_preference",
    },
))

# 4. 結束會話（觸發四層壓縮整合）
ums.observe(AgentEvent(
    event_type=EventType.SESSION_END,
    agent_context=ctx,
    content="會話結束",
))

# 5. 新會話 —— 檢索相關上下文
ctx2 = AgentContext(
    agent_id="pa_001",
    agent_type="personal_assistant",
    session_id="sess_2",
    user_id="alice",
)

memories = ums.recall("日本旅行旅館", context=ctx2, budget_tokens=1000)

# 6. 以上下文區塊形式注入到 LLM 提示詞中
context_block = ums.inject_context("日本旅行旅館", context=ctx2, budget_tokens=1000)
print(context_block)
```

**輸出：**
```
## 相關記憶上下文

1. [SEMANTIC] Alice 是素食者，喜歡精品旅館
2. [EPISODIC] [USER_INPUT] 我是素食者，而且我喜歡精品旅館。
```

---

## 七大記憶原語

UAMS 暴露 **7 個通用原語**，替代了 agentmemory 的 53 個編碼專用工具。任何智能體框架都透過這 7 個呼叫完成整合。

| 原語 | 簽名 | 用途 |
|------|------|------|
| **`observe(event)`** | 將任意 `AgentEvent` 記錄到工作記憶 | 主要擷取入口 —— 接入智能體生命週期 |
| **`remember(fact, ...)`** | 顯式將事實儲存到語意記憶 | 使用者直接陳述偏好或事實 |
| **`recall(query, ...)`** | 跨所有層級檢索相關記憶 | 每次智能體行動前呼叫，載入上下文 |
| **`forget(memory_id, cascade=...)`** | 刪除記憶,並按需求沿正向與反向引用級聯,同時寫入稽核軌跡。回傳 `CascadeReport` | GDPR「被遺忘權」/ 使用者請求 / 清理 |
| **`consolidate(session_id)`** | 觸發四層壓縮整合 | 會話結束自動觸發，或手動呼叫 |
| **`inject_context(...)`** | 將記憶格式化為提示詞文字區塊 | 直接注入到 LLM 系統提示詞 |
| **`sync(target)`** | 與外部檔案雙向同步 | `MEMORY.md`、遊戲存檔檔案等 |

---

## 四層記憶模型

UAMS 以人類認知架構為藍本建模記憶。每一層擁有獨立的儲存後端、檢索策略和遺忘曲線。

```
┌────────────────────────────────────────────────────────────┐
│  工作記憶 (WORKING)      原始事件、感官輸入           30分鐘 TTL │
│  ─────────────────────────────────────────────────────────  │
│  情境記憶 (EPISODIC)     會話敘事、經驗經歷           7天半衰期   │
│  ─────────────────────────────────────────────────────────  │
│  語意記憶 (SEMANTIC)     事實、偏好、概念             90天半衰期  │
│  ─────────────────────────────────────────────────────────  │
│  程序記憶 (PROCEDURAL)   技能、工作流程、模式           1年半衰期   │
└────────────────────────────────────────────────────────────┘
```

### 各層詳情

| 層級 | 儲存內容 | 預設 TTL | 檢索方式 | 範例 |
|------|---------|---------|---------|------|
| **工作記憶** | 原始 `AgentEvent` 流 | 30 分鐘 | 精確匹配 / 最近優先 | "使用者 2 分鐘前說了'你好'" |
| **情境記憶** | 壓縮後的會話摘要 | 7 天 | 關鍵字 + 語意 | "昨天的旅行規劃會話" |
| **語意記憶** | 擷取的事實和偏好 | 90 天 | 語意向量搜尋 | "Alice 是素食者" |
| **程序記憶** | 可複用的模式和策略 | 1 年 | 圖譜 + 模式匹配 | "處理旅行查詢時，先問飲食限制" |

### 記憶衰減公式（艾賓浩斯遺忘曲線）

```
留存率 = 0.5^(時間 / 半衰期)
         × (1 + 0.1 × 存取次數)      # 被存取的記憶強化
         × (0.5 + 0.5 × 重要性/10)   # 重要記憶持久化
         × 置信度                      # 被矛盾的記憶消退
```

如果 `留存率 < 留存閾值`，記憶將被自動驅逐。

---

## 🧠 LLM 壓縮(可選)

> **預設 = `HeuristicCompressionEngine` ≈ 0% token 節省。** UAMS 內建啟發式引擎,開箱即用不依賴 LLM;啟發式只做事件結構化 (`[TYPE] content\n...`),**不做摘要**。下面 72% 標題是 **LLM 模式** 的數字,透過環境變數顯式 opt-in。

預設關閉 —— UAMS 內建 **啟發式壓縮引擎**,無需 LLM 依賴即可執行。啟用 **LLM 壓縮** 可在長會話場景獲得真實 token 節省。

```bash
# OpenAI
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=sk-...
export UAMS_LLM_MODEL=gpt-4o-mini

# MiniMax (OpenAI 相容)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=<minimax-key>
export UAMS_LLM_BASE_URL=https://api.minimaxi.com/v1
export UAMS_LLM_MODEL=MiniMax-Text-01

# 本機 ollama(OpenAI 相容模式)
export UAMS_LLM_ENABLED=true
export UAMS_LLM_API_KEY=ollama        # 必填但不使用
export UAMS_LLM_BASE_URL=http://localhost:11434/v1
export UAMS_LLM_MODEL=llama3.1
```

**LLM 在壓縮各階段做什麼**:

| 階段 | 啟發式(預設) | LLM 壓縮 |
|------|--------------|---------|
| 情景記憶壓縮 | 拼接 `[TYPE] content\n...`(≈原始 token 數) | 摘要為約 200 字敘述(有界) |
| 語義記憶抽取 | 僅挑選 `(str/int/float/bool)` 結構化欄位 | LLM 抽取原子事實(JSON) |
| 程序模式識別 | 統計 category 出現次數(≥2) | LLM 識別重複工作流 |

**實測節省**(20 事件會話):

```
啟發式 (預設):  300 tokens  (原始 100%,≈ 0% 節省,僅做結構化)
LLM (opt-in):    84 tokens  (原始  28%)  → 72% 節省
```

如果 LLM 呼叫失敗(網路/配額/逾時),UAMS **自動降級**到啟發式壓縮,agent 主迴圈不會卡住。詳見 [docs/PR1-2-LLM-Compression.md](docs/PR1-2-LLM-Compression.md)。

---

## 🔌 可插拔 Embedding 提供方

預設關閉 —— UAMS 退化為 **BM25 + 圖譜檢索**(RRF 3 路中的 2 路)。啟用後可獲得完整混合檢索管線。

| 提供方 | 模式 | 安裝 | 適用場景 |
|--------|------|------|---------|
| **NoOp** | 無 | 內建 | 關閉向量檢索,純 BM25 + 圖譜 |
| **SentenceTransformers** | 本機 | `pip install "uams[embeddings]"` | 離線/內網部署,預設 `all-MiniLM-L6-v2`(384 維) |
| **OpenAI 相容** | 遠端 | `pip install "uams[llm]"` | OpenAI / MiniMax / ollama / vLLM(設定 `UAMS_EMBEDDING_BASE_URL`) |

```bash
# 本機 sentence-transformers
export UAMS_EMBEDDING_ENABLED=true
export UAMS_EMBEDDING_PROVIDER=sentence_transformers
export UAMS_EMBEDDING_MODEL=all-MiniLM-L6-v2

# 遠端 OpenAI 相容
export UAMS_EMBEDDING_ENABLED=true
export UAMS_EMBEDDING_PROVIDER=openai_compatible
export UAMS_EMBEDDING_API_KEY=<key>
export UAMS_EMBEDDING_BASE_URL=https://api.openai.com/v1
export UAMS_EMBEDDING_REMOTE_MODEL=text-embedding-3-small
```

所有提供方共享統一的 **LRU 緩存**(預設 5000 條),避免重複 embedding 呼叫。任何提供方初始化失敗都會降級到 NoOp 並打 WARNING 日誌 —— 檢索自動回退到 BM25 + 圖譜。

---

## 多智能體支援

UAMS 透過三個原語實現多智能體之間的協調：**租約（Lease）**、**訊號（Signal）** 和 **共享記憶空間**。

### 啟用多智能體模式

```python
ums.enable_multi_agent()  # 預設建立共享 InMemoryStore
```

### 資源租約（獨占鎖）

```python
# 智能體 A 取得獨占任務
acquired = ums.acquire_lock("agent_a", "task_001_analysis", ttl=300.0)
# 取得成功回傳 True，已被其他智能體鎖定則回傳 False

# 智能體 B 嘗試取得同一任務 —— 被阻擋
blocked = ums.acquire_lock("agent_b", "task_001_analysis")  # False

# 智能體 A 釋放鎖
ums.release_lock("agent_a", "task_001_analysis")
```

### 智能體間訊號

```python
from uams import Signal

# 智能體 A 向智能體 B 傳送訊息
ums.send_signal(Signal(
    sender="agent_a",
    recipient="agent_b",   # 使用 "*" 進行廣播
    signal_type="data_ready",
    payload={"dataset_size": 10000, "location": "/shared/data.csv"},
))

# 智能體 B 讀取所有未讀訊號
signals = ums.read_signals("agent_b")
for sig in signals:
    print(f"來自 {sig.sender}: {sig.type} - {sig.payload}")
```

### 共享記憶空間

```python
# 智能體 A 擷取資料並共享給團隊
ums.observe(AgentEvent(...))  # 寫入工作記憶

# 提升到團隊共享語意空間
ums.share_memory(memory, target_team="analysis_team")

# 智能體 B 查詢團隊上下文
team_memories = ums._coordinator.get_team_context("analysis_team", "dataset")
```

---

## 專案結構

```
universal-agent-memory/
├── pyproject.toml          # Python 套件設定
├── README.md               # 本文件（英文）
├── README.zh-CN.md         # 簡體中文版本
├── README.zh-TW.md         # 繁體中文版本（本文件）
├── src/uams/               # 核心套件（約 12200 行）
│   ├── system.py           # 主入口（forget() 級聯分派）
│   ├── async_system.py     # 異步 API
│   ├── config.py           # 配置 + 生產安全校驗
│   ├── benchmarks.py       # 效能基準
│   ├── health.py           # 健康檢查與指標
│   ├── core/               # 列舉、資料模型
│   ├── bus/                # 事件匯流排
│   ├── storage/            # 6 個儲存後端（InMemory/SQLite/PG/Redis/Neo4j/ChromaDB）
│   ├── pipeline/           # 壓縮、檢索、隱私、遺忘、LLM 壓縮、**級聯**
│   │   └── cascade.py      # **CascadeForgetter (BFS + visit-set + max_depth + best-effort)**
│   ├── multi_agent/        # 協調
│   ├── embedding/          # 嵌入介面 + 4 個 provider
│   ├── llm/                # OpenAI 相容 LLM 客戶端 + 快取
│   ├── adapters/           # 框架適配器
│   └── utils/              # 日誌、重試、安全、token、備份、**級聯稽核**
│       └── cascade_audit.py  # **追加式 JSONL 稽核寫入器（GDPR 軌跡）**
├── examples/               # 5 個領域範例 + token 壓縮示範
│   ├── personal_assistant.py
│   ├── game_npc.py
│   ├── customer_service.py
│   ├── research_agent.py
│   ├── multi_agent.py
│   └── _token_compression_demo.py
├── tests/                  # 375 個測試
│   ├── test_system.py
│   ├── test_chaos.py
│   ├── test_aplus.py
│   ├── test_postgresql_store.py    # CI：真實 PG service container
│   ├── test_chromadb_store.py      # CI：真實 ChromaDB EphemeralClient
│   ├── test_redis_store_real.py    # CI：真實 redis service
│   ├── test_neo4j_store_real.py    # CI：真實 neo4j service
│   ├── test_cascade.py             # 級聯刪除測試
│   ├── test_config_validation.py
│   ├── test_llm_compression.py
│   └── test_embedding.py
└── docs/                   # 文件
    ├── API.md              # API 參考
    ├── ARCHITECTURE.md     # 架構深讀
    ├── CASCADE_FORGET.md   # 級聯刪除使用者指南
    ├── DEPLOYMENT.md       # 部署指南
    ├── DEPLOYMENT.zh-CN.md # 部署指南（簡中）
    ├── PR1-2-LLM-Compression.md # LLM 壓縮交接文件
    └── superpowers/        # 規格 + 計畫（跨層級聯刪除）
```

---

## 範例

從專案根目錄直接執行任意範例：

```bash
# 個人助理：跨會話記住飲食偏好和旅館品味
python examples/personal_assistant.py

# 遊戲 NPC：酒館老闆記住玩家過去的不良行為
python examples/game_npc.py

# 客服：客服智能體召回同一客戶的過往工單
python examples/customer_service.py

# 科研智能體：文獻綜述智能體召回先前假設和關鍵論文
python examples/research_agent.py

# 多智能體：資料擷取智能體向分析智能體傳送訊號並共享資料集
python examples/multi_agent.py
```

---

## 測試

```bash
# 執行所有單元測試
python -m unittest discover -s tests -v

# 或直接執行測試腳本
python tests/test_system.py
```

### 已驗證的測試覆蓋

| 測試 | 驗證內容 |
|------|---------|
| MemoryId 唯一性 | 全域 UUID 產生 |
| 觀察 + 檢索 | 事件擷取與跨會話檢索 |
| 顯式記住 | 直接向語意層寫入事實 |
| 隱私過濾 | SECRET 脫敏和 PII 遮罩 |
| 去重 | SHA-256 滾動視窗防止重複擷取 |
| 多智能體鎖 | 獨占租約取得與阻擋 |
| 層級統計 | 工作/情境/語意/程序層計數正確 |
| 上下文注入 | 產生可直接用於提示詞的文字區塊 |
| **6 後端真實驗證(CI 9/9 green)** | **PG / ChromaDB / Redis / Neo4j / SQLite / InMemory 全部以真實 service container 跑通** |
| **級聯刪除** | **三策略 + visit-set + 最大深度上限 + 跨層隔離 + 最佳努力刪除 + JSONL 稽核** |

**測試規模**:375 個測試(本地 32 skip:無 PG/Redis/Neo4j service 時跳過真實後端;CI 全跑通)。

---

## 架構說明

### 記憶迴路

```
┌─────────────────┐     ┌──────────────────┐
│   智能體事件     │────▶│   事件匯流排      │
│   (任意領域)     │     │   (零耦合)        │
└─────────────────┘     └────────┬─────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
           ┌────────▼────────┐      ┌────────▼────────┐
           │ 隱私過濾器       │      │ 去重視窗         │
           │ (脫敏敏感資訊)    │      │ (SHA-256 視窗)  │
           └────────┬────────┘      └────────┬────────┘
                    │                         │
                    └────────────┬────────────┘
                                 │
                          ┌──────▼──────┐
                          │  工作記憶層   │  ← 30分鐘 TTL，精確匹配
                          │  (WORKING)   │
                          └──────┬──────┘
                                 │ 會話結束觸發整合
                    ┌────────────┴────────────┐
                    │                         │
           ┌────────▼────────┐      ┌────────▼────────┐
           │ 壓縮引擎        │      │ 壓縮引擎        │
           │ (LLM 驅動)      │      │ (規則/啟發式)  │
           └────────┬────────┘      └────────┬────────┘
                    │                         │
             ┌──────▼──────┐           ┌──────▼──────┐
             │  情境記憶層  │           │  語意記憶層  │
             │ (EPISODIC)  │           │ (SEMANTIC)  │
             │  7天半衰期   │           │  90天半衰期  │
             └─────────────┘           └──────┬──────┘
                                              │
                                       ┌──────▼──────┐
                                       │  程序記憶層  │
                                       │ (PROCEDURAL)│
                                       │  1年半衰期   │
                                       └─────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │      檢索管線            │
                    │ (BM25 + 向量 + 圖譜 + RRF)│
                    └────────────┬────────────┘
                                 │
                          ┌──────▼──────┐
                          │  Token 預算  │
                          │  壓縮        │
                          └──────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │   注入到智能體            │
                    │   提示詞 / 上下文視窗      │
                    └───────────────────────────┘
```

### 核心抽象

UAMS 對**你的領域一無所知**。它只知道：

- `AgentEvent` —— **誰**（智能體上下文）、**何時**（時間戳）、**什麼**（內容 + 結構化資料）
- `Memory` —— **ID**（UUID）、**時間錨點**（時間後設資料）、**上下文**（誰產生的）、**載荷**（原始 + 結構化 + 嵌入）、**後設資料**（類型 / 隱私 / 重要性 / 關係）

你的領域專用資訊全部存在於：
- `payload.raw` —— 自然語言描述
- `payload.structured` —— 可序列化的 JSON 工件
- `metadata.categories` —— 你自己的標籤（如 `travel_preference`、`player_reputation`、`paper_reference`）

---

## 安裝指南

### 從原始碼安裝（推薦用於開發）

```bash
git clone https://github.com/liwt2010/universal-agent-memory.git
cd universal-agent-memory
pip install -e .
```

### 生產環境：新增可插拔後端

```bash
# 向量搜尋（語意層）
pip install chromadb

# 知識圖譜（程序層）
pip install neo4j

# 本地嵌入（無需 API 金鑰）
pip install sentence-transformers
```

---

## 貢獻指南

我們歡迎來自所有領域的貢獻 —— 個人助理、遊戲 AI、機器人、客服、科研工具等。

1. Fork 儲存庫
2. 建立功能分支（`git checkout -b feature/awesome-feature`）
3. 提交變更（`git commit -m '新增 awesome 功能'`）
4. 推送到分支（`git push origin feature/awesome-feature`）
5. 發起 Pull Request

提交前請確保所有測試通過：

```bash
python -m unittest discover -s tests -v
```

---

## 語言版本

- [English](README.md)
- [简体中文 (Simplified Chinese)](README.zh-CN.md)
- [繁體中文 (Traditional Chinese)](README.zh-TW.md) （本文件）

---

## 授權條款

Apache-2.0

---

## 致謝

UAMS 受以下優秀專案的啟發：

- [agentmemory](https://github.com/rohitg00/agentmemory) by Rohit Ghumare —— 證明了該架構在編碼智能體上的可行性
- [MemGPT](https://github.com/cpacker/MemGPT) by Charles Packer —— 為 LLM 設計了作業系統級的記憶管理

UAMS 將它們的領域專用創新，泛化為一個通用的智能體基礎設施層。

---

<p align="center">
  <b>通用記憶。任意智能體。任意領域。</b>
</p>
