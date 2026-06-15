# 特征写入笔记

## 行为写入

`POST /api/v1/events` 写实时行为：

```json
{
  "user_id": "u_feature_demo",
  "behavior_type": "view",
  "item_id": "P218",
  "metadata": {
    "category": "耳机",
    "brand": "Sony"
  }
}
```

可以改：

- `behavior_type`：只能是 `view`、`click`、`purchase`。
- `item_id`：商品 ID 或行为对象 ID，例如 `P218`、`P128`。
- `metadata.category`：行为关联类目，例如 `耳机`、`手机`、`智能家居`。
- `metadata.brand`：行为关联品牌，例如 `Sony`、`Apple`、`华为`。
- `metadata.amount`：购买金额，做 `purchase` 事件时可以加，例如 `1299`。

写完后用 `GET /api/v1/users/{user_id}/features` 看：

- `view_count_1h`
- `view_count_24h`
- `recent_views`
- `recent_clicks`
- `recent_purchases`
- `source`

## 离线画像写入

`POST /api/v1/users/{user_id}/offline-tags` 写离线画像：

```json
{
  "tags": {
    "preferred_categories_30d": ["耳机"],
    "preferred_brands_30d": ["Sony"],
    "price_sensitivity": 0.9,
    "churn_risk_score": 0.2
  }
}
```

可以改：

- `preferred_categories_30d`：长期偏好类目。
- `preferred_brands_30d`：长期偏好品牌。
- `price_sensitivity`：价格敏感度，建议 `0.1` 到 `0.9`。
- `churn_risk_score`：流失风险，建议 `0.0` 到 `1.0`。

写完后同样用 `GET /api/v1/users/{user_id}/features` 看 `offline_tags` 是否合并进去。
