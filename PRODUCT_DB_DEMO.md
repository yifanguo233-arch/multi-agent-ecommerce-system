# 商品库改造演示说明

本项目现在不再依赖运行时内置 mock 商品做主链路召回。默认配置会使用：

```text
ECOM_DATABASE_URL=sqlite:///./ecommerce.db
ECOM_PRODUCT_AUTO_SEED=true
ECOM_PRODUCT_SEED_COUNT=240
```

启动服务时如果 `products` 表为空，会自动 seed 100-500 条演示商品，默认 240 条。也可以手动执行：

```powershell
.\.venv\Scripts\python.exe scripts\seed_products.py --count 240
```

Postgres 可把连接串改成类似：

```text
ECOM_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/ecommerce
```

## 面试演示链路

1. 看 DB 状态：

```text
GET /health
```

返回里的 `database.product_count` 会显示商品表记录数。

2. 查商品表：

```text
GET /api/v1/products?limit=5
GET /api/v1/products/P015
```

3. 查库存：

```text
GET /api/v1/products/P015/inventory
```

4. 跑推荐：

```text
POST /api/v1/recommend
POST /api/v1/recommend/graph
```

推荐返回的 `products[*].product_id` 可以继续用 `/api/v1/products/{product_id}` 回查真实商品记录。

`/api/v1/recommend/graph` 还会返回 `agent_results`，其中：

- `product_recall.data.product_source = "database"`
- `product_recall.data.database_url` 显示当前数据源
- `product_recall.data.candidate_product_ids` 显示候选商品 ID
- `inventory.data.stock_source = "database"`
- `inventory.data.stock_snapshot` 显示库存 Agent 从 DB 读到的库存

这条链路可以说明：API 请求进入 LangGraph/Supervisor，ProductRecAgent 从商品表召回，InventoryAgent 通过 `product_id` 回查库存，最终推荐结果可追踪回数据库商品行。
