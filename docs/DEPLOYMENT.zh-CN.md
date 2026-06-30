# UAMS 部署与运维手册

## 目录

- [快速开始](#快速开始)
- [环境变量配置](#环境变量配置)
- [存储后端选型](#存储后端选型)
- [Docker 部署](#docker-部署)
- [健康检查与监控](#健康检查与监控)
- [多 Agent 分布式部署](#多-agent-分布式部署)
- [性能调优](#性能调优)
- [故障排查](#故障排查)

---

## 快速开始

### 1. 纯内存模式（开发测试）

```bash
pip install -e .
python -c "
from uams.system import UniversalMemorySystem
from uams.health import HealthServer

ums = UniversalMemorySystem()
server = HealthServer(port=3111)
server.start(ums_instance=ums)
print('UAMS running on http://localhost:3111')
"
```

### 2. SQLite 持久化模式（单机生产）

```bash
export UAMS_STORAGE_BACKEND=sqlite
export UAMS_SQLITE_PATH=/data/uams.db
export UAMS_LOG_LEVEL=INFO

python -c "
from uams.system import UniversalMemorySystem
from uams.health import HealthServer

ums = UniversalMemorySystem()
server = HealthServer(port=3111)
server.start(ums_instance=ums)
"
```

### 3. Docker Compose 一键启动

```bash
docker compose up -d
```

默认启用 SQLite 后端，数据持久化到 `uams-data` volume。

---

## 环境变量配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `UAMS_STORAGE_BACKEND` | `memory` | 存储后端：`memory` / `sqlite` / `redis` / `neo4j` |
| `UAMS_SQLITE_PATH` | `uams.db` | SQLite 数据库文件路径 |
| `UAMS_REDIS_HOST` | `localhost` | Redis 主机地址 |
| `UAMS_REDIS_PORT` | `6379` | Redis 端口 |
| `UAMS_REDIS_DB` | `0` | Redis 数据库编号 |
| `UAMS_REDIS_PASSWORD` | — | Redis 密码（可选） |
| `UAMS_REDIS_PREFIX` | `uams:memory:` | Redis key 前缀 |
| `UAMS_REDIS_TTL` | — | Redis 全局 TTL（秒，可选） |
| `UAMS_REDIS_PUBSUB` | `false` | 是否启用 Redis Pub/Sub 信号队列 |
| `UAMS_NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt 地址 |
| `UAMS_NEO4J_USER` | `neo4j` | Neo4j 用户名 |
| `UAMS_NEO4J_PASSWORD` | `password` | Neo4j 密码 |
| `UAMS_NEO4J_DATABASE` | `neo4j` | Neo4j 数据库名 |
| `UAMS_LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `UAMS_HEALTH_PORT` | `3111` | 健康检查 HTTP 端口 |
| `UAMS_EVENT_BUS_MAX_BUFFER` | `1000` | 事件总线缓冲区大小 |
| `UAMS_WORKING_TTL` | `1800` | Working 层 TTL（秒） |
| `UAMS_RRF_K` | `60` | RRF 融合参数 K |

---

## 存储后端选型

| 后端 | 适用场景 | 持久化 | 多进程 | 图遍历 | 向量搜索 | 信号队列 |
|------|---------|--------|--------|--------|----------|----------|
| **memory** | 开发/测试/演示 | ❌ | ❌ | ❌ | ❌ | ❌ |
| **sqlite** | 单机生产/中小规模 | ✅ | ❌ | 有限 | ❌ | ❌ |
| **redis** | 分布式缓存/高并发 | ✅ | ✅ | ❌ | ❌ | ✅ |
| **neo4j** | 复杂关系/知识图谱 | ✅ | ✅ | ✅ | ❌ | ❌ |
| **chromadb** | 向量语义检索 | ✅ | ✅ | ❌ | ✅ | ❌ |

### 推荐组合

- **单机全功能**：`sqlite` + `InMemoryStore(WORKING)`
- **分布式缓存**：`redis`（所有层级）+ 定期 `decay_sweep()`
- **知识图谱**：`neo4j`（所有层级）+ 图关系建模
- **语义检索**：`chromadb`（SEMANTIC 层）+ `sqlite`（EPISODIC/PROCEDURAL）

### 切换后端示例

```bash
# Redis 后端
export UAMS_STORAGE_BACKEND=redis
export UAMS_REDIS_HOST=redis.cluster.local
export UAMS_REDIS_PORT=6380
export UAMS_REDIS_PUBSUB=true

# Neo4j 后端
export UAMS_STORAGE_BACKEND=neo4j
export UAMS_NEO4J_URI=bolt://neo4j.prod:7687
export UAMS_NEO4J_PASSWORD=secure_password
```

---

## Docker 部署

### 基础部署（SQLite）

```bash
docker compose up -d
```

### Redis 后端部署

```bash
docker compose -f docker-compose.yml -f docker-compose.redis.yml up -d
```

### Neo4j 后端部署

```bash
docker compose -f docker-compose.yml -f docker-compose.neo4j.yml up -d
```

### 自定义镜像构建

```bash
docker build -t uams:latest .
docker run -d \
  -p 3111:3111 \
  -e UAMS_STORAGE_BACKEND=sqlite \
  -e UAMS_SQLITE_PATH=/data/uams.db \
  -v uams-data:/data \
  uams:latest
```

---

## 健康检查与监控

### 健康端点

| 端点 | 说明 | 示例 |
|------|------|------|
| `GET /health` | 系统健康状态 | `{"status": "healthy"}` |
| `GET /ready` | 就绪检查 | `{"status": "ready"}` |
| `GET /metrics` | Prometheus 指标 | `uams_requests_total 42` |
| `GET /stats` | 内存统计 | `{"WORKING": 5, "EPISODIC": 12}` |

### 容器健康检查

Dockerfile 内置了 healthcheck：
```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3111/health')" || exit 1
```

### 自定义监控集成

```python
from uams.health import MetricsCollector

collector = MetricsCollector()
collector.increment("custom_event")
collector.histogram("latency_ms", 45.2)

# 获取 Prometheus 格式输出
print(collector.format_prometheus())
```

---

## 多 Agent 分布式部署

### 架构概述

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Agent A   │◄───►│   Redis     │◄───►│   Agent B   │
│  (Worker)   │     │ (Pub/Sub)   │     │  (Worker)   │
└─────────────┘     └─────────────┘     └─────────────┘
                            │
                     ┌──────┴──────┐
                     │  SQLite/    │
                     │   Neo4j     │
                     │ (Persistent)│
                     └─────────────┘
```

### 启用分布式信号

```python
from uams.system import UniversalMemorySystem

ums = UniversalMemorySystem(config=UAMSConfig(
    storage_backend="redis",
    redis_enable_pubsub=True,
))

# 发送跨进程信号
ums.send_signal(Signal(sender="agent_a", recipient="agent_b", signal_type="task_complete"))

# 接收信号
signals = ums.read_signals("agent_b")
```

### 资源锁（Lease）

```python
# 获取分布式锁
if ums.acquire_lock("agent_a", "dataset_001", ttl=300):
    try:
        # 独占处理数据
        pass
    finally:
        ums.release_lock("agent_a", "dataset_001")
```

---

## 性能调优

### 1. Working 层缓存

Working 层默认使用 `InMemoryStore`（热缓存），即使其他层级使用 Redis/Neo4j。如需修改，可在 `system.py` 中自定义 `_init_stores_from_config`。

### 2. SQLite WAL 模式

SQLite 默认启用 WAL（Write-Ahead Logging），支持读写并发。对于极高并发场景，建议切换到 Redis。

### 3. Token 预算控制

```python
# 减少检索预算以降低延迟
results = ums.recall(query, context=ctx, budget_tokens=1000)
```

### 4. 定期清理过期记忆

```python
import time

while True:
    time.sleep(300)
    ums.decay_sweep()  # 清理过期 Working 记忆
```

### 5. 批量操作

```python
# 使用事件总线批量处理
for event in events:
    ums.observe(event)
```

---

## 故障排查

### 启动失败

| 现象 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError: redis` | 未安装 redis 依赖 | `pip install redis` 或 `pip install redis neo4j` |
| `Neo4j connection failed` | Neo4j 服务未启动 | 检查 `docker compose -f docker-compose.neo4j.yml up` |
| `SQLite database locked` | 并发写入冲突 | 启用 WAL 或切换到 Redis |

### 查询缓慢

| 现象 | 原因 | 解决 |
|------|------|------|
| `recall` 返回慢 | 数据量大且无索引 | 切换到 SQLite/Neo4j，确保索引已创建 |
| 高 CPU 占用 | 频繁 graph 遍历 | 限制 graph depth=1 或缓存关系 |
| 内存增长 | Working 层未清理 | 调用 `decay_sweep()` 或降低 TTL |

### 数据丢失

| 现象 | 原因 | 解决 |
|------|------|------|
| 重启后记忆消失 | 使用 memory 后端 | 切换到 sqlite/redis/neo4j |
| 部分记忆丢失 | ForgettingEngine 清理 | 调整 importance > 7.0 以保留 |

---

## 升级指南

### 从 memory 升级到 sqlite

```bash
# 1. 停止服务
# 2. 导出记忆（如有）
# 3. 设置环境变量
export UAMS_STORAGE_BACKEND=sqlite
export UAMS_SQLITE_PATH=/data/uams.db
# 4. 重启服务
```

### 从 sqlite 升级到 redis

```bash
# 1. 启动 Redis 服务
docker compose -f docker-compose.yml -f docker-compose.redis.yml up -d redis
# 2. 切换环境变量
export UAMS_STORAGE_BACKEND=redis
export UAMS_REDIS_HOST=redis
# 3. 重启 UAMS 服务
# 4. 数据需手动迁移（如有历史数据）
```

---

## 附录：生产检查清单

- [ ] 已选择持久化后端（sqlite/redis/neo4j）
- [ ] 已配置日志级别（INFO 或 WARNING）
- [ ] 已启用健康检查端口（3111）
- [ ] 已设置隐私过滤规则（如处理敏感数据）
- [ ] 已配置定期 `decay_sweep()` 或 TTL
- [ ] 已备份 SQLite/Neo4j 数据卷
- [ ] 已测试故障降级（断开 Redis/Neo4j 观察 graceful 行为）
