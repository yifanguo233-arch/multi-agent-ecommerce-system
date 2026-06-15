
# 1. 在项目根目录启动服务
cd C:\my_project\multi-agent-ecommerce-system
docker compose up -d

# 2. 打开演示页面
Start-Process "http://127.0.0.1:8000/docs"
```

刚改过代码或 Dockerfile：

```powershell
docker compose up -d --build
```

## Swagger 页面演示

### 1. 实现的功能

- 个性化推荐：根据用户上下文和场景返回商品列表
- 商品召回与重排：结合 query、用户偏好、热门度、价格、库存、多样性等因素筛选候选
- 库存过滤：最终推荐结果会排除不可售或库存不足商品
- 营销文案生成：为每个推荐商品生成一条文案
- 实时行为特征：可接入 Redis 记录浏览、点击、购买等行为
- 长期画像特征：支持写入长期偏好与价格敏感度等画像标签
- 可追踪结果：返回执行策略、节点链路、耗时、错误和 replay key
- 实验与指标：支持 A/B 分桶、outcome 回写、metrics 观测

### 2. 健康检查

### 3. 商品列表

### 4. 库存查询

### 5. 推荐链路

[推荐链路调参笔记](./interview-condition-cheatsheet.md)
[推荐响应字段解读笔记](./interview-response-fields-guide.md)


演示用例 A：通勤降噪耳机

```json
{
  "user_id": "u_audio_commute_demo1",
  "scene": "homepage",
  "num_items": 5,
  "business_goal": "conversion",
  "context": {
    "query": "通勤降噪耳机",
    "recent_views": ["耳机", "降噪", "通勤", "Sony"],
    "avg_order_amount": 1800,
    "trigger_event": "browse"
  }
}
```

演示用例 B：办公 4K 显示器

```json
{
  "user_id": "u_monitor_office_demo1",
  "scene": "homepage",
  "num_items": 5,
  "business_goal": "conversion",
  "context": {
    "query": "办公4K显示器",
    "recent_views": ["显示器", "4K", "办公", "Dell"],
    "avg_order_amount": 3000,
    "trigger_event": "browse"
  }
}
```
### 6. Replay

### 7. 写入用户行为和画像特征

[特征写入笔记](./interview-feature-write-guide.md)

先回到 `POST /api/v1/recommend/graph`，用这份弱上下文跑一次，记住结果：

```json
{
  "user_id": "u_feature_compare_demo",
  "scene": "homepage",
  "num_items": 5,
  "business_goal": "conversion",
  "context": {
    "avg_order_amount": 1800,
    "trigger_event": "browse"
  }
}
```
写一条浏览行为：

```json
{
  "user_id": "u_feature_compare_demo",
  "behavior_type": "view",
  "item_id": "P218",
  "metadata": {
    "category": "耳机",
    "brand": "Sony"
  }
}
```

写一条点击行为：

```json
{
  "user_id": "u_feature_compare_demo",
  "behavior_type": "click",
  "item_id": "P004",
  "metadata": {
    "category": "耳机",
    "brand": "Sony"
  }
}
```
 `POST /api/v1/users/{user_id}/offline-tags`

 `user_id` 填：
```text
u_feature_compare_demo
```

请求体填：

```json
{
  "tags": {
    "preferred_categories_30d": ["耳机"],
    "preferred_brands_30d": ["Sony"],
    "price_sensitivity": 0.9
  }
}
```

 `GET /api/v1/users/{user_id}/features`：

```text
user_id = u_feature_compare_demo
```


### 8. 再跑一次推荐链路

```json
{
  "user_id": "u_feature_compare_demo",
  "scene": "homepage",
  "num_items": 5,
  "business_goal": "conversion",
  "context": {
    "avg_order_amount": 1800,
    "trigger_event": "browse"
  }
}
```

### 9. Metrics

### 10. A/B 实验

```json
{
  "user_id": "demo_control",
  "scene": "homepage",
  "num_items": 3,
  "business_goal": "conversion",
  "context": {
    "recent_views": ["gaming", "audio"],
    "trigger_event": "browse"
  }
}
```
