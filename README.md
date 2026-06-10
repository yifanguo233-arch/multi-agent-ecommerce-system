# Multi-Agent E-Commerce Recommendation System

> 基于开源项目 [bcefghj/multi-agent-ecommerce-system](https://github.com/bcefghj/multi-agent-ecommerce-system) 的 Python 后端二次改造版本。

本仓库不是对原项目的简单复制，而是在参考原项目 Multi-Agent 电商推荐思路的基础上，把 Python 方向整理成一个可以本地运行、可以测试、可以通过接口演示的推荐工作流后端。项目重点展示我在 Agent 编排、LangGraph 工作流、数据库商品库、可观测性、降级兜底和工程化启动方面的改造能力。

## 我做了哪些改造

原项目是一个面向学习和面试的多语言示例项目，包含 Python / Java / Go 三套说明和大量面试材料。本仓库当前聚焦 Python 后端，并对目录、运行方式和推荐链路做了工程化整理。

主要改造点如下：

- 项目结构整理：将 Python 项目从 `python/` 子目录迁移到仓库根目录，保留核心后端代码、测试、Docker 配置和运行文档，降低阅读和运行成本。
- FastAPI 服务化：提供统一 HTTP API，包括推荐、商品查询、库存查询、行为事件、用户特征、实验、指标和 LangGraph replay。
- LangGraph 工作流：把推荐过程拆成初始化、计划生成、工具路由、用户画像、商品召回、重排、库存过滤、文案生成、反思和结果聚合等节点。
- Supervisor 降级链路：保留 Supervisor 编排器作为降级路径，当 Graph 初始化或执行异常时，推荐接口仍可以返回可用结果。
- 数据库商品库：增加 `ProductRepository`，使用 SQLAlchemy 管理商品表，默认 SQLite，本地启动时自动 seed 240 条 demo 商品；Docker Compose 场景使用 Postgres。
- 库存回查：`InventoryAgent` 通过商品 ID 从数据库读取库存，推荐结果可以追溯到真实商品记录。
- 用户上下文与实时特征：增加 `MemoryContextEngine` 和可选 Redis FeatureStore，支持行为事件写入、离线标签合并和用户特征聚合。
- LLM + 规则兜底：Planner、用户画像、商品重排、营销文案支持 OpenAI-compatible LLM；当 API Key 缺失、超时或 JSON 解析失败时，自动走规则 fallback。
- RAG/向量检索：增加本地 lexical/NumPy/FAISS 风格检索能力，用商品文档构建召回候选，辅助推荐链路。
- A/B 实验：增加哈希分桶、实验组配置和 Thompson Sampling outcome 更新，用于演示推荐策略实验。
- 可观测性：返回 agent 调用结果、trace steps、node latency，并提供 `/api/v1/metrics` 和 `/metrics` Prometheus 格式指标。
- Checkpoint Replay：使用 `langgraph-checkpoint-sqlite` 持久化图状态，支持按 `thread_id` 或 `request_id` 回放一次推荐链路。
- 测试覆盖：增加/保留 37 个单元测试，覆盖 Planner、FeatureStore、RAG、商品库、Graph、Supervisor、LLM repair 等关键模块。
- 本地运行体验：增加 `.env.example`、Dockerfile、Compose、seed 脚本、smoke test，降低项目启动和演示成本。

## 当前功能

这个项目当前实现的是一个电商推荐工作流后端，不是完整电商平台。一次推荐请求的大致流程如下：

```text
用户请求
  |
  v
FastAPI /api/v1/recommend
  |
  v
LangGraph 推荐工作流
  |
  +--> PlannerAgent          生成执行计划
  +--> ToolRouter            根据计划选择工具
  +--> UserProfileAgent      构建用户画像
  +--> ProductRecAgent       从商品库/RAG/规则召回候选商品
  +--> ProductRecAgent       可选 LLM 重排
  +--> InventoryAgent        数据库库存过滤
  +--> MarketingCopyAgent    生成个性化营销文案
  +--> Metrics/Trace         记录耗时、状态和调试信息
  |
  v
推荐结果 + 文案 + 实验组 + debug_context
```

核心能力：

- 多 Agent 推荐链路：Planner、用户画像、商品推荐、库存、文案等 Agent 分工协作。
- LangGraph 状态图：支持节点化编排、条件分支、trace 和 checkpoint。
- 数据库商品目录：默认 SQLite，支持 Postgres 连接串。
- 自动 seed demo 商品：空库启动时自动生成 240 条商品。
- Redis 可选实时特征：Redis 不可用时主推荐链路仍然可用。
- LLM 可选增强：配置 API Key 后启用画像、计划、重排、文案能力；未配置时走规则。
- 推荐结果可追踪：返回候选商品、库存来源、Agent 结果、Graph 节点耗时等调试信息。
- A/B 实验和指标：可以查看实验分组、记录 outcome、查看内存指标和 Prometheus 指标。
- Checkpoint replay：可以回放 LangGraph 的执行状态。

## 技术栈

| 模块 | 技术 |
| --- | --- |
| Web 框架 | FastAPI, Uvicorn |
| Agent 编排 | LangGraph, asyncio |
| LLM 接入 | LangChain, langchain-openai, OpenAI-compatible API |
| 数据模型 | Pydantic v2 |
| 数据库 | SQLite, PostgreSQL, SQLAlchemy |
| 实时特征 | Redis |
| 检索 | lexical search, NumPy vector search, optional FAISS style backend |
| 指标 | in-memory metrics, prometheus-client |
| 测试 | pytest, pytest-asyncio |
| 部署 | Docker, Docker Compose |

## 目录结构

```text
.
├── main.py                         # FastAPI 入口和 HTTP API
├── config/
│   └── settings.py                 # 环境变量配置，前缀 ECOM_
├── models/
│   └── schemas.py                  # 请求、响应、商品、用户画像、执行计划等模型
├── agents/
│   ├── base_agent.py               # Agent 基类：重试、超时、fallback、耗时统计
│   ├── planner_agent.py            # 推荐计划 Agent
│   ├── user_profile_agent.py       # 用户画像 Agent
│   ├── product_rec_agent.py        # 商品召回和重排 Agent
│   ├── inventory_agent.py          # 库存过滤 Agent
│   └── marketing_copy_agent.py     # 营销文案 Agent
├── orchestrator/
│   ├── graph.py                    # LangGraph 状态图
│   └── supervisor.py               # Supervisor 降级编排器
├── services/
│   ├── product_repository.py       # 商品库、库存查询、demo seed
│   ├── memory_context.py           # 用户上下文构建
│   ├── feature_store.py            # Redis FeatureStore
│   ├── auto_planner.py             # 规则 + LLM 的执行计划生成
│   ├── rag_vector_search.py        # RAG/向量检索
│   ├── ab_test.py                  # A/B 实验和 Thompson Sampling
│   ├── metrics.py                  # 内存指标和 Prometheus 输出
│   ├── sqlite_checkpoint.py        # LangGraph checkpoint/replay index
│   └── tool_registry.py            # 工具注册与执行
├── scripts/
│   ├── seed_products.py            # 手动 seed demo 商品
│   ├── run_workflow_demo.py        # 工作流 demo
│   ├── evaluate_recommendation.py  # 推荐评估脚本
│   └── smoke_test.ps1              # 冒烟测试脚本
├── tests/                          # 单元测试
├── Dockerfile
├── compose.yaml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## 快速启动

### 1. 创建虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

如果仓库里已经有 `.venv`，可以直接使用：

```powershell
.\.venv\Scripts\python.exe --version
```

### 2. 配置环境变量

```powershell
Copy-Item .env.example .env
```

主要配置项：

```text
ECOM_LLM_API_KEY                  LLM API Key；不填也可以运行，系统会走规则 fallback
ECOM_LLM_BASE_URL                 OpenAI-compatible API 地址
ECOM_LLM_MODEL                    使用的模型名
ECOM_PLANNER_USE_LLM              Planner 是否尝试调用 LLM
ECOM_REDIS_URL                    Redis 地址
ECOM_DATABASE_URL                 商品库地址，默认 sqlite:///./ecommerce.db
ECOM_PRODUCT_AUTO_SEED            商品表为空时是否自动 seed
ECOM_PRODUCT_SEED_COUNT           demo 商品数量，默认 240
ECOM_AB_TEST_ENABLED              是否启用默认 A/B 实验
ECOM_ORCHESTRATION_MODE           graph 或 supervisor，默认 graph
ECOM_CHECKPOINT_BACKEND           checkpoint 后端，当前支持 sqlite
ECOM_CHECKPOINT_SQLITE_PATH       checkpoint SQLite 文件路径
ECOM_RAG_ENABLED                  是否启用 RAG 检索
```

### 3. 启动服务

```powershell
.\.venv\Scripts\uvicorn.exe main:app --host 127.0.0.1 --port 8000 --reload
```

打开健康检查：

```text
GET http://127.0.0.1:8000/health
```

首次启动时，如果 `ecommerce.db` 中没有商品，系统会自动 seed demo 商品。

### 4. 可选：启动 Redis

Redis 不是主推荐接口的硬依赖。没有 Redis 时，推荐接口仍然可用；行为事件和用户特征接口会提示 Redis 不可用。

```powershell
docker run --name ecommerce-redis -p 6379:6379 -d redis:7
```

## Docker Compose 启动

Compose 会启动 API、Postgres 和 Redis。注意：当前 `compose.yaml` 要求 `.env` 中配置 `ECOM_LLM_API_KEY`，否则会直接启动失败，用来避免误以为 LLM 已启用。

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写 ECOM_LLM_API_KEY
docker compose up -d --build
docker compose ps
```

服务地址：

```text
API:      http://127.0.0.1:8000
Health:   http://127.0.0.1:8000/health
Redis:    localhost:6379
Postgres: localhost:5432
```

## API 示例

### 健康检查

```http
GET /health
```

返回内容会包含模型、Redis 状态、数据库商品数量和 checkpoint 配置。

### 查询商品

```http
GET /api/v1/products?limit=5
GET /api/v1/products/P015
GET /api/v1/products/P015/inventory
```

### 推荐接口

```http
POST /api/v1/recommend
Content-Type: application/json
```

```json
{
  "user_id": "user_001",
  "scene": "homepage",
  "num_items": 3,
  "business_goal": "conversion",
  "context": {
    "query": "running shoes",
    "recent_views": ["sports", "shoes"],
    "avg_order_amount": 300
  }
}
```

返回中重点看：

```json
{
  "request_id": "...",
  "user_id": "user_001",
  "products": [],
  "marketing_copies": [],
  "experiment_group": "control",
  "execution_plan": {},
  "debug_context": {
    "graph_thread_id": "...",
    "agent_results": {},
    "trace_steps": [],
    "node_latency_ms": {},
    "graph_errors": []
  }
}
```

### LangGraph 专用接口

```http
POST /api/v1/recommend/graph
GET  /api/v1/recommend/graph/replay/{thread_id}
GET  /api/v1/recommend/graph/replay/by-request/{request_id}
```

这些接口主要用于展示工作流状态、checkpoint 和 replay 能力。

### Redis FeatureStore 接口

```http
POST /api/v1/events
GET  /api/v1/users/{user_id}/features
POST /api/v1/users/{user_id}/offline-tags
```

Redis 不可用时，这些接口会返回 503，但不会影响 `/api/v1/recommend` 的基础推荐链路。

### 实验与指标

```http
GET  /api/v1/experiments
POST /api/v1/experiments/{experiment_id}/outcome?group=control&success=true
GET  /api/v1/metrics
GET  /metrics
```

## 测试

```powershell
.\.venv\Scripts\pytest.exe -q
```

当前本地验证结果：

```text
37 passed
```

已覆盖的重点模块：

- `AutoPlanner` 规则计划和 LLM JSON repair
- `UserProfileAgent`、`ProductRecAgent`、`MarketingCopyAgent` 的 fallback/repair
- `ProductRepository` 商品库 seed、查询和库存读取
- `MemoryContextEngine` 用户上下文构建
- Redis FeatureStore 行为聚合
- RAG/向量检索召回
- Supervisor 编排
- LangGraph pipeline、工具输出、checkpoint replay
- A/B 实验和 Thompson Sampling

## 项目边界

当前项目适合用于学习、面试展示和后端工程演示，但还不是生产级电商系统。

未实现或仅 demo 级实现的部分：

- 没有前端页面、商品运营后台、订单、支付、购物车、账号和权限系统。
- 商品数据是 demo seed 数据，不是真实业务商品库。
- 推荐模型不是训练好的工业推荐模型，主要是规则、RAG、LLM 重排和 workflow 展示。
- Redis、metrics、A/B outcome 默认都是演示级能力，部分状态保存在内存中。
- 库存只做查询和过滤，不支持锁库存、扣减库存和并发一致性。
- `CORS` 当前适合本地演示，不建议直接公网暴露。
- 没有生产级鉴权、限流、审计、租户隔离和数据脱敏。

## 参考与致谢

- 原始参考项目：[https://github.com/bcefghj/multi-agent-ecommerce-system](https://github.com/bcefghj/multi-agent-ecommerce-system)
- 本仓库是在该项目思路基础上进行的 Python 后端二次改造，README 中已明确标注来源和本人主要改造内容。
