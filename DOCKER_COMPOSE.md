# Docker Compose 一键启动

在仓库根目录执行：

```powershell
docker compose up -d --build
```

启动后会得到三组服务：

- `api`: FastAPI 推荐服务，端口 `8000`
- `redis`: 用户实时特征存储，端口 `6379`
- `postgres`: 商品库和库存表，端口 `5432`

## 大模型配置

当前 Compose 会读取项目根目录的 `.env`，并把大模型配置传给 `api` 容器：

```text
ECOM_LLM_API_KEY
ECOM_LLM_BASE_URL
ECOM_LLM_MODEL
ECOM_PLANNER_USE_LLM
ECOM_PLANNER_LLM_TIMEOUT_SECONDS
ECOM_AGENT_TIMEOUT_PLANNER
ECOM_PLANNER_LLM_MAX_TOKENS
```

`compose.yaml` 已经要求 `ECOM_LLM_API_KEY` 必须存在。也就是说，如果 `.env` 没有配置 key，`docker compose up` 会直接报错，而不是悄悄禁用大模型。

Compose 同时会把 API 配成：

```text
ECOM_DATABASE_URL=postgresql+psycopg://ecommerce:ecommerce@postgres:5432/ecommerce
ECOM_REDIS_URL=redis://redis:6379/0
ECOM_PRODUCT_AUTO_SEED=true
ECOM_PRODUCT_SEED_COUNT=240
ECOM_PRODUCT_CATALOG_CACHE_LIMIT=500
ECOM_ORCHESTRATION_MODE=graph
ECOM_PLANNER_USE_LLM=${ECOM_PLANNER_USE_LLM:-true}
ECOM_RAG_ENABLED=${ECOM_RAG_ENABLED:-false}
```

`ECOM_RAG_ENABLED` 默认保持 `false`，避免在没有单独配置 embedding 模型时影响主推荐链路。大模型画像、Planner、重排和文案链路会使用 `.env` 里的 LLM 配置。

## 验证

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/products?limit=3"
```

推荐链路：

```powershell
$body = @{
  user_id = "u_compose_demo"
  scene = "homepage"
  num_items = 3
  business_goal = "conversion"
  context = @{
    recent_views = @("手机", "耳机")
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/recommend/graph" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

可以在响应里看这些字段确认大模型链路：

- `agent_results.planner.data.llm_called`
- `agent_results.user_profile.data.llm_called`
- `agent_results.product_recall.data.llm_called`
- `agent_results.marketing_copy.data.llm_called`
- `agent_results.product_recall.data.fallback_reason`
- `agent_results.marketing_copy.data.fallback_reason`

查看日志：

```powershell
docker compose logs -f api
```

停止服务：

```powershell
docker compose down
```

清空 Postgres/Redis 数据卷并重新 seed：

```powershell
docker compose down -v
docker compose up -d --build
```
