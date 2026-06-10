# Multi-Agent E-Commerce Recommendation System 项目说明

本项目是一个用 Python 编写的电商推荐工作流后端示例。它的核心不是一个完整电商平台，而是一个“多 Agent 推荐流程运行时”：接收推荐请求，构建用户上下文，生成执行计划，召回和重排商品，做库存过滤，生成营销文案，并返回可观察的推荐结果。

代码里已经实现了 FastAPI 接口、LangGraph 工作流、Supervisor 降级编排、数据库商品目录、Redis 可选实时特征、A/B 分组、指标统计、Checkpoint Replay、RAG 风格检索和若干 Agent。它可以本地运行、调试和演示一条推荐链路，但商品、实验、指标和库存能力仍然是 demo 级实现，不是生产级推荐系统。

## 1. 项目能做什么

这个项目目前可以做到：

- 启动一个 FastAPI 服务，提供商品查询、推荐、行为事件、用户特征、实验、指标和 replay 接口。
- 对一次推荐请求生成执行计划，包括召回策略、重排重点、文案语气、风险策略等。
- 使用 LangGraph 把推荐流程拆成多个节点执行，并记录每个节点的 trace 和耗时。
- 使用多个 Agent 分别处理用户画像、商品推荐、库存过滤、营销文案和计划生成。
- 在配置了 OpenAI-compatible LLM API Key 时，让 Planner、画像、重排和文案 Agent 调用大模型。
- 在 LLM 不可用、超时或返回格式无法解析时，走规则兜底，尽量返回结构合法的结果。
- 使用 `ProductRepository` 管理 demo 商品库；默认 SQLite，本地或 Docker Compose 可自动 seed 240 条商品。
- 提供商品列表、商品详情、商品库存查询接口，方便排查推荐结果来源。
- 使用本地词频向量检索，或者在有 API Key 时尝试使用 embedding + NumPy/FAISS 做 RAG 检索。
- 可选连接 Redis，把用户浏览、点击、购买事件写入 Redis，并聚合成实时特征。
- 返回 A/B 实验分组，并支持记录实验 outcome 来更新内存中的 Thompson Sampling 状态。
- 暴露内存指标和 Prometheus 格式指标，方便观察请求数、Agent 调用数和延迟。
- 通过官方 `langgraph-checkpoint-sqlite` 持久化 LangGraph checkpoint，支持重启后按 `thread_id` replay 图工作流请求。
- 运行单元测试，验证 Planner、MemoryContext、FeatureStore、RAG、Graph、Supervisor 等模块。

## 2. 项目不能做什么

这个项目现在不能被理解成完整的线上电商系统。它目前不能做到：

- 没有前端页面、后台管理系统、商品管理界面或运营配置界面。
- 没有真实业务商品库或商品运营后台；当前商品来自 demo seed 数据，默认写入 SQLite，Docker Compose 场景写入 Postgres。
- 没有真实订单、支付、购物车、用户账号、权限系统。
- 没有训练好的推荐模型，也没有真实协同过滤模型；代码注释里提到的“协同过滤”当前只是推荐流程概念，实际主路径使用数据库商品、规则排序、RAG 检索和可选 LLM 重排。
- 没有真正接入 Milvus。依赖和配置里保留了 Milvus 字段，但当前主链路没有查询 Milvus collection。
- 库存不是生产级库存服务；当前 `InventoryAgent` 会从商品表批量读取 stock，查不到时才回退到商品对象字段，不支持锁库存、扣减库存或并发一致性。
- Redis 是可选依赖。Redis 不可用时，推荐链路仍可用，但行为事件写入、用户特征查询、离线标签合并接口会返回 503。
- Replay 是本地持久化调试能力。当前 LangGraph checkpoint 使用官方 SQLite checkpointer，`request_id -> thread_id` 索引也默认写入 SQLite，但还不是完整审计系统。
- 指标不是持久化监控系统。`/api/v1/metrics` 的聚合数据在内存中，服务重启后清空。
- A/B 实验不是完整实验平台。默认分组和 outcome 状态在内存里，没有实验配置后台、统计显著性分析或持久化结果。
- 营销文案合规检查只是硬编码敏感词替换，不能替代法律审核。
- 没有鉴权、限流、租户隔离、审计日志、数据脱敏等生产安全能力。
- `CORS` 当前允许所有来源，适合本地演示，不适合直接暴露公网。

## 3. 目录结构

```text
main.py                         FastAPI 入口，注册 HTTP 接口和应用生命周期
config/
  settings.py                   环境变量配置
models/
  schemas.py                    请求、响应、用户、商品、计划、Agent 结果模型
agents/
  base_agent.py                 Agent 基类，提供重试、耗时统计和 fallback
  planner_agent.py              推荐执行计划 Agent
  user_profile_agent.py         用户画像 Agent
  product_rec_agent.py          商品召回和重排 Agent
  inventory_agent.py            库存过滤 Agent
  marketing_copy_agent.py       营销文案 Agent
orchestrator/
  graph.py                      LangGraph 状态图工作流
  supervisor.py                 Supervisor 编排器，作为可选/降级路径
services/
  auto_planner.py               规则 + 可选 LLM 的计划生成器
  memory_context.py             用户上下文构建
  feature_store.py              Redis FeatureStore
  redis_runtime.py              Redis 连接初始化
  product_repository.py         SQLAlchemy 商品库、库存读取和 demo seed
  rag_vector_search.py          本地 RAG/向量检索实现
  tool_registry.py              工具注册与执行
  ab_test.py                    A/B 实验和 Thompson Sampling 状态
  metrics.py                    内存指标和 Prometheus 指标
scripts/
  run_workflow_demo.py          LangGraph 工作流逐步输出 demo
  smoke_test.ps1                启动服务并跑一组冒烟检查
tests/
  test_*.py                     单元测试
requirements.txt                运行依赖
requirements-dev.txt            开发/测试依赖
Dockerfile                      简单容器构建文件
compose.yaml                    API、Redis、Postgres 一键启动配置
.env.example                    环境变量示例
```

## 4. 启动方式

建议在项目的 `python` 目录执行命令。

### 4.1 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 4.2 配置环境变量

```powershell
Copy-Item .env.example .env
```

主要配置项：

```text
ECOM_LLM_API_KEY                  LLM API Key；不填时 Planner 会走规则，其他 LLM Agent 调用失败后走 fallback
ECOM_LLM_BASE_URL                 OpenAI-compatible API 地址
ECOM_LLM_MODEL                    使用的模型名
ECOM_PLANNER_USE_LLM              Planner 是否尝试使用 LLM
ECOM_REDIS_URL                    Redis 地址
ECOM_DATABASE_URL                 商品库地址；默认 sqlite:///./ecommerce.db
ECOM_PRODUCT_AUTO_SEED            商品表为空时是否自动 seed demo 商品
ECOM_PRODUCT_SEED_COUNT           demo 商品 seed 数量；默认 240
ECOM_PRODUCT_CATALOG_CACHE_LIMIT  推荐 Agent 缓存的商品目录上限
ECOM_AB_TEST_ENABLED              是否启用默认 A/B 实验
ECOM_ORCHESTRATION_MODE           graph 或 supervisor；默认 graph
ECOM_CHECKPOINT_BACKEND           checkpoint 后端；当前支持 sqlite
ECOM_CHECKPOINT_SQLITE_PATH       SQLite checkpoint 文件路径；默认 ./ecommerce_checkpoints.db
ECOM_RAG_ENABLED                  是否启用 embedding RAG 尝试
ECOM_RAG_BACKEND                  auto、faiss、numpy、lexical
```

`.env.example` 里也有 Milvus 配置，但当前主链路没有实际查询 Milvus collection。商品目录和库存读取会使用 `ECOM_DATABASE_URL` 指向的数据库。

### 4.3 可选启动 Redis

Redis 不是推荐接口的硬性依赖，但行为事件和用户特征接口依赖 Redis。

```powershell
docker run --name ecommerce-redis -p 6379:6379 -d redis:7
```

### 4.4 启动 API 服务

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

健康检查：

```text
GET http://127.0.0.1:8000/health
```

如果 Redis 连不上，`/health` 仍可能返回 `healthy`，但 `redis.enabled` 会是 `false`，并带有错误信息。

## 5. 核心数据模型

主要模型都在 `models/schemas.py`。

### 5.1 推荐请求

```json
{
  "user_id": "u001",
  "scene": "homepage",
  "num_items": 5,
  "business_goal": "conversion",
  "context": {
    "recent_views": ["手机", "耳机"],
    "purchase_count_30d": 2,
    "avg_order_amount": 699.0
  }
}
```

字段说明：

- `user_id`：用户 ID。
- `scene`：推荐场景，例如 `homepage`、`detail`、`pdp`。
- `num_items`：希望返回的商品数量。
- `business_goal`：业务目标，目前只是传入计划和响应，没有完整的目标优化系统。
- `context`：请求上下文，可放近期浏览、点击、价格敏感度、离线标签、强制 reflection 等信息。

### 5.2 推荐响应

响应里通常包含：

- `request_id`：请求 ID。
- `user_id`：用户 ID。
- `products`：推荐商品列表。
- `marketing_copies`：每个商品的营销文案。
- `experiment_group`：A/B 实验分组。
- `plan_version`：计划版本。
- `execution_plan` / `plan_payload`：Planner 和工具节点生成或修改后的执行计划。
- `planner_observability`：Planner 的模式、fallback 原因等。
- `debug_context`：调试上下文。
- `agent_results`：各 Agent 的执行结果、fallback 状态、错误信息等。
- `total_latency_ms`：总耗时。

`/api/v1/recommend/graph` 会返回更偏图工作流调试的字段，包括 `thread_id`、`trace_steps`、`node_latency_ms`、`errors`。

## 6. HTTP 接口说明

### 6.1 `GET /health`

用于检查服务是否启动，并查看当前模型名、Redis、数据库和 checkpoint 状态。

返回示例：

```json
{
  "status": "healthy",
  "model": "MiniMax-M1",
  "redis": {
    "enabled": false,
    "error": "..."
  },
  "database": {
    "url": "sqlite:///./ecommerce.db",
    "product_count": 240,
    "seeded": 0,
    "error": ""
  },
  "checkpoint": {
    "backend": "sqlite",
    "sqlite_path": "./ecommerce_checkpoints.db"
  }
}
```

### 6.2 `GET /api/v1/products`

查看 demo 商品库。支持分页、按类目过滤和只看有库存商品：

```text
GET /api/v1/products?limit=20&offset=0&category=手机&in_stock_only=true
```

返回里会包含：

- `source`：当前为 `database`。
- `database_url`：脱敏后的商品库地址。
- `total_count`：商品表总数。
- `products`：商品列表。

### 6.3 `GET /api/v1/products/{product_id}`

查看单个商品详情。商品不存在时返回 404。

### 6.4 `GET /api/v1/products/{product_id}/inventory`

查看单个商品当前库存。返回 `stock` 和 `available`。

### 6.5 `POST /api/v1/recommend`

统一推荐入口。

默认情况下使用 `ECOM_ORCHESTRATION_MODE=graph`，先走 LangGraph。如果图工作流抛异常，会降级到 `SupervisorOrchestrator`。如果配置为 `supervisor`，则直接使用 Supervisor 编排。

示例：

```powershell
$body = @{
  user_id = "u001"
  scene = "homepage"
  num_items = 5
  business_goal = "conversion"
  context = @{
    recent_views = @("手机", "耳机")
    purchase_count_30d = 2
    avg_order_amount = 699.0
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/recommend" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

### 6.6 `POST /api/v1/recommend/graph`

只走 LangGraph 工作流，不做 Supervisor 降级。这个接口更适合观察图节点、trace、耗时和 replay。

它会返回：

- `request_id`
- `thread_id`
- `products`
- `marketing_copies`
- `plan_payload`
- `trace_steps`
- `node_latency_ms`
- `errors`

### 6.7 `GET /api/v1/recommend/graph/replay/{thread_id}`

根据官方 SQLite checkpointer 持久化的 LangGraph checkpoint 状态读取 `thread_id`。默认文件是 `./ecommerce_checkpoints.db`，可通过 `ECOM_CHECKPOINT_SQLITE_PATH` 调整。

### 6.8 `GET /api/v1/recommend/graph/replay/by-request/{request_id}`

根据业务 `request_id` 找到对应的 `thread_id`，再调用 replay。这个索引和 checkpoint 使用同一个 SQLite 文件，服务重启后仍可查询。

### 6.9 `POST /api/v1/events`

写入用户行为事件到 Redis FeatureStore。

请求示例：

```json
{
  "user_id": "u001",
  "behavior_type": "view",
  "item_id": "P001",
  "metadata": {
    "category": "手机",
    "brand": "Apple"
  }
}
```

`behavior_type` 只能是：

- `view`
- `click`
- `purchase`

如果 Redis 不可用，这个接口返回 503。

### 6.10 `GET /api/v1/users/{user_id}/features`

读取 Redis 聚合后的用户特征，包括浏览次数、点击次数、购买次数、近期兴趣、RFM 和离线标签。

如果 Redis 不可用，这个接口返回 503。

### 6.11 `POST /api/v1/users/{user_id}/offline-tags`

把离线标签写入 Redis，例如：

```json
{
  "tags": {
    "preferred_categories_30d": ["手机", "配件"],
    "preferred_brands_30d": ["Apple"],
    "price_sensitivity": 0.8,
    "churn_risk_score": 0.2
  }
}
```

这些标签会被 `MemoryContextEngine` 和 `UserProfileAgent` 读取，用来影响后续推荐流程。

### 6.12 `GET /api/v1/experiments`

查看内置实验配置和内存统计。

默认有两个实验：

- `rec_strategy`：推荐策略实验，包含 `control` 和 `treatment_llm`。
- `copy_style`：文案风格实验，包含 `formal` 和 `casual`。

主推荐链路当前使用的是 `ab_engine.assign()`，也就是基于 `user_id` 的一致性哈希分组。

### 6.13 `POST /api/v1/experiments/{experiment_id}/outcome`

记录某个实验组的一次成功或失败，用于更新内存中的 Thompson Sampling 成功/失败计数。

示例：

```text
POST /api/v1/experiments/rec_strategy/outcome?group=control&success=true
```

注意：主链路当前没有自动使用 `assign_thompson()` 分流，所以这个接口更多是演示 Thompson Sampling 状态更新能力。

### 6.14 `GET /api/v1/metrics`

返回内存里的 Agent 调用统计和业务事件统计。

包含：

- 每个 Agent 的调用次数。
- 成功率。
- 平均延迟。
- 最近错误。

### 6.15 `GET /metrics`

返回 Prometheus 文本格式指标，包含请求数、请求耗时、Agent 调用数、Agent 耗时。

## 7. LangGraph 工作流

主工作流在 `orchestrator/graph.py`。

执行顺序大致是：

```text
init
  -> planner
  -> tool_router
  -> tool_executor
  -> user_profile + product_recall
  -> merge_phase1
  -> rerank + inventory
  -> merge_phase2
  -> filter
  -> inventory_repair / retention_offer / marketing_copy
  -> marketing_copy
  -> reflection / aggregate
```

节点职责：

- `init`：生成请求初始状态，做 A/B 分组，初始化 trace 和错误列表。
- `planner`：构建用户上下文，调用 PlannerAgent 生成执行计划。
- `tool_router`：根据计划选择策略工具。
- `tool_executor`：执行工具并更新 `plan_payload`。
- `user_profile`：生成用户画像。
- `product_recall`：召回候选商品。
- `merge_phase1`：等待画像和召回完成。
- `rerank`：根据画像、上下文和计划重排商品。
- `inventory`：检查库存，生成可售商品 ID、低库存提醒和限购信息。
- `merge_phase2`：等待重排和库存完成。
- `filter`：把重排结果和库存可用结果合并，得到最终商品列表。
- `inventory_repair`：当最终商品为空时，用原始候选商品做兜底。
- `retention_offer`：当用户上下文包含高流失风险时，给 context 加 `retention_mode`。
- `marketing_copy`：为最终商品生成文案。
- `reflection`：当结果质量不够时调整计划并回到工具路由。
- `aggregate`：计算总耗时并结束。

### Reflection 什么时候触发

`route_after_marketing_copy()` 里定义了触发条件：

- `context.force_reflection = true`。
- 最终商品为空。
- 文案数量少于商品数量。
- 商品数量不少于 2，但品类只有 1 个，认为多样性不足。
- 还没有达到 `max_reflections`。

Reflection 不会重新跑完整 Planner，而是在当前 `plan_payload` 上做调整：

- 商品太少：改成 `retrieve_strategy = hot_first`。
- 文案数量不匹配：改成 `copy_tone = rational`。
- 多样性不足：改成 `rerank_focus = diversity`。

然后回到 `tool_router`，重跑后续链路。默认最多 reflection 一次。

## 8. Supervisor 编排路径

Supervisor 在 `orchestrator/supervisor.py`。

它不是图工作流，而是一个更直接的异步编排器：

```text
构建用户上下文
  -> PlannerAgent
  -> 并行执行 UserProfileAgent 和 ProductRecAgent 初次召回
  -> 并行执行 ProductRecAgent 重排 和 InventoryAgent 库存检查
  -> 根据库存过滤最终商品
  -> MarketingCopyAgent 生成文案
  -> 汇总 RecommendationResponse
```

Supervisor 的作用：

- 当 `ECOM_ORCHESTRATION_MODE=supervisor` 时作为主路径。
- 当 `/api/v1/recommend` 的 graph 路径异常时作为降级路径。

Supervisor 没有 LangGraph 的 checkpoint replay，也没有 graph 节点级 trace。

## 9. Agent 说明

### 9.1 BaseAgent

文件：`agents/base_agent.py`

提供所有 Agent 的基础能力：

- 记录调用次数和错误次数。
- 计算耗时。
- 使用 `tenacity` 做重试。
- 出错时返回结构合法的 fallback `AgentResult`。

它只封装执行机制，不包含具体业务逻辑。

### 9.2 PlannerAgent 和 AutoPlanner

文件：

- `agents/planner_agent.py`
- `services/auto_planner.py`

Planner 的作用是把 `scene`、`business_goal`、`user_context`、`request_context` 转成 `ExecutionPlan`。

计划字段包括：

- `retrieve_strategy`：`semantic_first`、`hot_first`、`inventory_first`、`hybrid`。
- `rerank_focus`：`price_first`、`brand_first`、`diversity`、`balanced`、`intent_match`。
- `copy_tone`：`rational`、`promotion`、`new_release`、`reassure`、`default`。
- `risk_policy`：`retention_boost`、`stock_guard`、`standard`。
- `business_goal`：业务目标透传。
- `filters`：规则生成的过滤/偏置标记。
- `metadata`：场景、触发事件、Planner 模式、错误等。

规则逻辑举例：

- `scene` 是 `detail` 或 `pdp`，或者触发事件是 `detail_view` / `add_to_cart`，会倾向 `semantic_first` 和 `intent_match`。
- 价格敏感度高，会倾向 `price_first` 和 `promotion`。
- 高流失风险，会倾向 `retention_boost` 和 `reassure`。
- 首页场景会加 `diversity_boost`。

LLM 使用方式：

- 如果 `ECOM_PLANNER_USE_LLM=true` 且配置了 `ECOM_LLM_API_KEY`，Planner 会在规则计划基础上请求 LLM 给出建议。
- LLM 返回值必须解析为允许范围内的字段，非法值会被忽略。
- LLM 超时或解析失败时，保留规则计划，并在 metadata 里记录 `planner_mode = rule_fallback`。
- 没有 API Key 时，Planner 直接 `rule_only`。

### 9.3 UserProfileAgent

文件：`agents/user_profile_agent.py`

用户画像 Agent 负责根据行为和上下文生成 `UserProfile`。

输入来源：

- Redis FeatureStore 中的实时特征。
- `MemoryContextEngine` 构建出来的 `user_context`。
- 请求里的 `context`。

合并优先级：

- Redis 和离线标签有数据时会参与合并。
- `user_context` 会提供短期兴趣、长期偏好、价格敏感度等。
- 请求 `context` 提供兜底默认值。

输出画像包含：

- 用户分群：`new_user`、`active`、`high_value`、`price_sensitive`、`churn_risk`。
- 偏好品类。
- 价格区间。
- RFM 分数。
- 实时标签。

LLM 不可用时，规则兜底会根据购买次数、浏览次数、客单价等生成简化画像。这个画像只是演示级推断，不是经过训练或校准的真实用户画像模型。

### 9.4 ProductRecAgent

文件：`agents/product_rec_agent.py`

商品推荐 Agent 负责候选商品召回和重排。

当前商品来源：

- `ProductRepository` 读取 `ECOM_DATABASE_URL` 指向的商品表。
- 默认本地使用 `sqlite:///./ecommerce.db`。
- Docker Compose 场景使用 Postgres：`postgresql+psycopg://ecommerce:ecommerce@postgres:5432/ecommerce`。
- `ECOM_PRODUCT_AUTO_SEED=true` 时，商品表为空会自动 seed demo 商品，默认 240 条。
- `MOCK_PRODUCTS` 仍保留为单元测试和兼容 fixture，运行时主路径会读数据库商品。
- demo 商品包括手机、耳机、平板、配件、笔记本、显示器、存储、穿戴、无人机、游戏机、智能家居、摄影等。

召回逻辑：

- 默认从数据库商品目录开始，并按配置的 catalog cache limit 做本地缓存。
- 如果计划是 `semantic_first` 或 `hybrid`，会尝试基于用户上下文构建 query 做 RAG 检索。
- 如果 embedding RAG 可用，会尝试 OpenAI-compatible embedding + FAISS/NumPy。
- 如果 embedding RAG 不可用，会降级到本地 `InMemoryVectorIndex` 词频检索。
- 如果计划是 `inventory_first`，按库存排序。
- 如果计划是 `hot_first`，按热度相关 tag 和库存排序。
- 如果上下文或画像有偏好品类，会把匹配品类排前。

重排逻辑：

- 有画像或 `user_context` 时，会构造候选商品摘要，请 LLM 返回商品 ID 排序。
- 支持对模型返回的 JSON 数组做容错解析。
- 如果模型第一次返回无法解析，会再请求一次格式修复。
- 如果仍失败，按候选顺序兜底。
- 如果没有画像也没有 `user_context`，不会调用 LLM，直接返回候选前 N 个。

需要注意：

- 当前是 demo 商品数据库，不是接入真实业务商品中心。
- 当前没有真实召回模型。
- 当前没有真实“买了 A 也买了 B”的协同过滤数据。
- 依赖和配置中有 Milvus，但主链路未连接 Milvus。

### 9.5 InventoryAgent

文件：`agents/inventory_agent.py`

库存 Agent 根据商品表里的 `stock` 字段做过滤和预警。如果数据库里查不到某个商品的库存，才回退到商品对象自带的 `stock`。

逻辑：

- `stock <= 0` 的商品不会加入可售列表。
- `stock <= 50` 标记为 `critical`，建议 `urgent_restock`。
- `stock <= 100` 标记为 `warning`，建议 `plan_restock`。
- 对低库存或热门商品生成限购数量。
- 如果 `risk_policy = retention_boost` 且库存足够，会放松部分限购。

限制：

- 不是生产库存服务。
- 没有锁库存、扣减库存、库存预占或并发一致性控制。
- 只是基于商品表库存快照做推荐前过滤和预警。

### 9.6 MarketingCopyAgent

文件：`agents/marketing_copy_agent.py`

营销文案 Agent 为每个最终商品生成一条文案。

逻辑：

- 根据用户分群选择 prompt 模板。
- 根据计划或请求 context 里的 `copy_tone` 调整语气。
- 调用 LLM 生成 JSON 数组格式文案。
- 如果 LLM 调用失败或输出无法解析，使用规则文案兜底。
- 对硬编码敏感词做替换。

限制：

- 文案质量依赖 LLM。
- 兜底文案比较简单，并且是英文模板。
- 合规检查只是字符串替换，不代表实际广告法合规审核。

## 10. 服务层说明

### 10.1 MemoryContextEngine

文件：`services/memory_context.py`

这个模块把 Redis 或请求 context 中的用户行为转换成统一的 `UserContextSnapshot`。

包含：

- `short_term`：近 1 小时浏览、点击、近 24 小时购买、短期意图品类。
- `long_term`：30 天偏好品类、偏好品牌、价格敏感度、客单价、购买力层级、流失风险。
- `intent`：触发事件、当前商品、关注品类。
- `preference`：偏好品类、品牌、价格敏感度、价格区间提示。
- `risk_flags`：低活跃、高流失、高价格敏感等风险标签。

### 10.2 FeatureStore

文件：`services/feature_store.py`

Redis FeatureStore 负责：

- 用 Redis Sorted Set 存用户行为事件。
- 按时间窗口读取最近行为。
- 聚合 `view_count_1h`、`view_count_24h`、`click_count_1h`、`purchase_count_7d`。
- 提取近期浏览、点击、购买兴趣。
- 用启发式方法计算 RFM。
- 合并离线标签。

它只在 Redis 可用时工作。

### 10.3 ProductRepository

文件：`services/product_repository.py`

这个模块负责 demo 商品库：

- 用 SQLAlchemy 管理 `products` 表。
- 支持 SQLite 和 Postgres 等 SQLAlchemy URL。
- 商品表为空时按 `ECOM_PRODUCT_SEED_COUNT` 自动生成 demo 商品。
- 提供商品列表、按 ID 查询、批量库存查询、类目列表等能力。
- `ProductRecAgent` 用它读取推荐候选商品，`InventoryAgent` 用它读取库存快照。

### 10.4 AutoPlanner

文件：`services/auto_planner.py`

AutoPlanner 是 PlannerAgent 的底层实现，负责：

- 先用规则生成基础计划。
- 如果配置允许并且有 API Key，再请求 LLM 给出建议。
- 解析 LLM 返回，支持去掉 `<think>...</think>`、提取 JSON block、从混合文本里提取字段。
- 只接受白名单里的策略值。
- LLM 失败时回到规则计划。

### 10.5 ToolRegistry

文件：`services/tool_registry.py`

这是一个进程内工具注册器。目前在 `orchestrator/graph.py` 注册了三个工具：

- `promote_hot`：把 `retrieve_strategy` 调整为 `hot_first`。
- `boost_diversity`：把 `rerank_focus` 调整为 `diversity`。
- `retention_guard`：把 `copy_tone` 调整为 `reassure`，把 `risk_policy` 调整为 `retention_boost`。

工具只是修改计划 payload，不是外部工具调用系统。

### 10.6 RAG 和向量检索

文件：`services/rag_vector_search.py`

包含：

- `InMemoryVectorIndex`：基于词频和余弦相似度的轻量检索。
- `OpenAIEmbeddingProvider`：OpenAI-compatible embedding 封装。
- `NumpyVectorStore`：NumPy 向量相似度检索。
- `FaissVectorStore`：FAISS 向量检索封装。
- `ModernRAGRetriever`：embedding + 向量库的检索器。
- `build_product_docs()`：把 Product 转成检索文档。

当前 RAG 的文档来源是 `ProductRecAgent` 缓存的数据库商品目录；`MOCK_PRODUCTS` 主要保留给测试兼容。

### 10.7 ABTestEngine

文件：`services/ab_test.py`

提供：

- 默认实验注册。
- 基于 `user_id` 和 `experiment_id` 的一致性哈希分桶。
- Thompson Sampling 状态更新。
- 简单内存指标记录和聚合。

主推荐链路使用 `assign()` 做一致性哈希分组，不是自动 Thompson Sampling 动态分流。

### 10.8 MetricsCollector

文件：`services/metrics.py`

提供：

- Agent 调用次数、成功率、平均耗时、最近错误。
- 业务事件计数。
- Prometheus Counter 和 Histogram。

这些指标都在当前进程内维护，不是持久化指标系统。

## 11. 配置说明

配置类在 `config/settings.py`，环境变量前缀是 `ECOM_`。

常用配置：

```text
ECOM_APP_NAME
ECOM_DEBUG

ECOM_LLM_API_KEY
ECOM_LLM_BASE_URL
ECOM_LLM_MODEL
ECOM_LLM_TEMPERATURE
ECOM_LLM_MAX_TOKENS

ECOM_REDIS_URL
ECOM_FEATURE_TTL_SECONDS

ECOM_MILVUS_HOST
ECOM_MILVUS_PORT
ECOM_MILVUS_COLLECTION

ECOM_DATABASE_URL
ECOM_PRODUCT_AUTO_SEED
ECOM_PRODUCT_SEED_COUNT
ECOM_PRODUCT_CATALOG_CACHE_LIMIT

ECOM_AB_TEST_ENABLED
ECOM_AB_TEST_DEFAULT_BUCKET_COUNT

ECOM_AGENT_TIMEOUT_USER_PROFILE
ECOM_AGENT_TIMEOUT_PRODUCT_REC
ECOM_AGENT_TIMEOUT_MARKETING_COPY
ECOM_AGENT_TIMEOUT_INVENTORY
ECOM_AGENT_TIMEOUT_PLANNER

ECOM_RAG_ENABLED
ECOM_RAG_BACKEND
ECOM_RAG_TOP_K
ECOM_EMBEDDING_MODEL

ECOM_PLANNER_USE_LLM
ECOM_PLANNER_LLM_TIMEOUT_SECONDS
ECOM_PLANNER_LLM_MAX_TOKENS

ECOM_ORCHESTRATION_MODE
ECOM_CHECKPOINT_BACKEND
ECOM_CHECKPOINT_SQLITE_PATH
```

## 12. 典型调试流程

### 12.1 看服务状态

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -Method Get
```

### 12.2 请求一次图推荐

```powershell
$body = @{
  user_id = "u_demo_001"
  scene = "homepage"
  num_items = 3
  business_goal = "conversion"
  context = @{
    recent_views = @("手机", "配件")
    price_sensitivity = 0.8
    avg_order_amount = 699
  }
} | ConvertTo-Json -Depth 8

$result = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/recommend/graph" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body

$result
```

### 12.3 Replay 这次请求

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/recommend/graph/replay/$($result.thread_id)" `
  -Method Get
```

### 12.4 强制触发 Reflection

```powershell
$body = @{
  user_id = "u_demo_002"
  scene = "homepage"
  num_items = 3
  business_goal = "conversion"
  context = @{
    force_reflection = $true
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/recommend/graph" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

观察返回里的：

- `trace_steps`
- `plan_payload`
- `selected_tools`
- `node_latency_ms`
- `errors`

### 12.5 写入 Redis 行为并查看特征

需要先启动 Redis。

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/events" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"user_id":"u_demo_001","behavior_type":"view","item_id":"P001","metadata":{"category":"手机","brand":"Apple"}}'

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/users/u_demo_001/features" `
  -Method Get
```

## 13. 脚本

### 13.1 工作流 demo

```powershell
.\.venv\Scripts\python.exe scripts\run_workflow_demo.py --user-id u001 --scene homepage --num-items 5
```

这个脚本会用 `graph.astream(..., stream_mode="updates")` 打印每个节点的状态变化摘要。

### 13.2 冒烟测试

```powershell
.\scripts\smoke_test.ps1 -Port 8000 -UserId u001 -NumItems 5
```

这个脚本会：

- 启动 uvicorn。
- 检查 `/health`。
- 调用 `/api/v1/recommend`。
- 调用 `/api/v1/recommend/graph`。
- 查看 `/api/v1/experiments`。
- 查看 `/api/v1/metrics`。
- 最后停止服务。

### 13.3 离线推荐评测

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_recommendation.py --num-users 30 --k 5
```

这个脚本会生成一批模拟用户行为，跑推荐链路，并输出：

- `coverage`：推荐结果覆盖了多少商品库。
- `diversity`：每次推荐结果中的类目多样性。
- `hit_rate@k`：推荐 Top K 是否命中模拟 holdout 商品。
- `fallback_rate`：Agent 或链路是否走 fallback。
- `avg_latency_ms` / `p95_latency_ms`：平均和 P95 延迟。

也可以评测已启动的 HTTP 服务：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_recommendation.py --mode api --api-url http://127.0.0.1:8000 --num-users 30 --k 5
```

## 14. 测试

运行全部测试：

```powershell
.\.venv\Scripts\pytest.exe
```

当前测试覆盖的方向包括：

- A/B 分组一致性和实验统计。
- AutoPlanner 规则、LLM 输出解析和 fallback。
- FeatureStore 的 Redis 行为聚合。
- MemoryContext 的请求 fallback 和 FeatureStore 数据读取。
- RAG 检索排序。
- ProductRecAgent 如何受上下文影响。
- LangGraph 路由、checkpoint、tool outputs、trace。
- PlannerAgent 超时 fallback。
- Supervisor 对上下文和计划字段的透传。

## 15. Docker / Docker Compose

推荐演示方式是一条命令启动 API、Redis 和 Postgres：

```powershell
docker compose up
```

启动后访问：

```text
GET http://127.0.0.1:8000/health
GET http://127.0.0.1:8000/api/v1/products?limit=3
POST http://127.0.0.1:8000/api/v1/recommend/graph
```

`compose.yaml` 会把 API 配到 `postgres` 商品库和 `redis` 特征存储，并在商品表为空时自动 seed 240 条商品。详细演示命令见 `DOCKER_COMPOSE.md`。

项目包含一个简单 `Dockerfile`：

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

单独使用 `Dockerfile` 时只会启动 API；使用 `docker compose up --build` 时会同时启动 Redis 和 Postgres。

## 16. 推荐链路里的 fallback

项目里有多个 fallback 设计，目的都是让本地 demo 不因为外部依赖失败就完全不可用。

### Planner fallback

- 没有 API Key：`rule_only`。
- LLM 超时或错误：`rule_fallback`。
- PlannerAgent 本身失败：返回默认 `ExecutionPlan`。

### UserProfile fallback

- LLM 画像失败：根据浏览、购买次数、客单价等生成规则画像。

### ProductRec fallback

- embedding RAG 不可用：使用本地词频检索。
- LLM 重排失败：使用候选商品原顺序。
- 没有画像和用户上下文：不调用 LLM，直接返回候选前 N 个。

### MarketingCopy fallback

- LLM 调用失败：生成简单规则文案。
- LLM 返回不能解析：生成简单规则文案。

### Graph fallback

- `/api/v1/recommend` 的 graph 路径异常时，降级到 Supervisor。
- `/api/v1/recommend/graph` 不降级，适合定位 graph 问题。

## 17. 适合怎么介绍这个项目

可以这样描述：

> 这是一个电商推荐工作流后端示例，用 FastAPI 暴露接口，用 LangGraph 管理推荐请求的状态流转，用多个 Agent 分别处理用户画像、计划生成、商品召回重排、库存过滤和文案生成。它支持数据库 demo 商品库、可选 LLM、规则 fallback、Redis 实时特征、A/B 分组、节点级 trace、Prometheus 指标和官方 SQLite checkpointer 持久化 replay。因此它更适合学习、演示和验证 Agentic Recommendation Workflow 的工程结构，而不是直接作为生产推荐系统。

## 18. 当前最重要的边界

如果要继续做成更完整的系统，优先要补的不是更多 prompt，而是这些基础能力：

- 把 demo 商品库接入真实商品中心、类目体系和运营配置。
- 接入真实用户行为流和离线画像。
- 把 Redis 特征、指标、A/B 实验结果持久化。
- 接入真实库存服务，支持锁库存、预占、扣减和并发一致性。
- 明确 Milvus 或其他向量库的生产链路。
- 增加鉴权、限流、日志脱敏和错误审计。
- 对推荐效果做离线评估和线上实验统计。
- 对文案输出做更严格的安全和合规校验。

在当前状态下，这个项目最适合用来说明“多 Agent 推荐工作流如何拆分、编排、观测和兜底”，而不是宣称它已经具备完整电商推荐平台能力。
