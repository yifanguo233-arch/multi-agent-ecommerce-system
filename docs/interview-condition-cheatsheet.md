# 条件速查表


## 一、最小请求模板

```json
{
  "user_id": "u_feature_demo",
  "scene": "homepage",
  "num_items": 3,
  "business_goal": "conversion",
  "context": {
    "avg_order_amount": 300,
    "trigger_event": "browse"
  }
}
```

## 二、最常用输入条件

### 1. 顶层字段

- `user_id`：用户标识。会影响 A/B 分桶、Redis 特征读取、用户画像拼装。
- `scene`：推荐场景，比如 `homepage`、`detail`、`pdp`。
- `num_items`：期望返回商品数。
- `business_goal`：业务目标，比如 `conversion`、`retention`、`gmv`、`ctr`。
- `context`：本次请求附带的即时上下文，是最常调的入口。

### 2. `context` 里最关键的字段

- `trigger_event`：如 `browse`、`detail_view`、`add_to_cart`
- `current_item`：当前浏览商品，常见于详情页
- `recent_views`：近期浏览类目或商品兴趣词
- `recent_clicks`：近期点击兴趣
- `recent_purchases`：近期购买兴趣
- `avg_order_amount`：客单价
- `price_sensitivity`：价格敏感度，越高越偏低价/促销
- `preferred_categories_30d`：长期偏好类目
- `preferred_brands_30d`：长期偏好品牌
- `churn_risk_score`：流失风险分
- `force_reflection`：强制走一轮 reflection

## 三、条件怎么影响 Planner

Planner 先产出 `ExecutionPlan`，它最重要的字段有：

- `retrieve_strategy`
- `rerank_focus`
- `copy_tone`
- `risk_policy`
- `filters`

### 1. 场景与触发事件

当：

- `scene in ("detail", "pdp")`
- 或 `trigger_event in ("detail_view", "add_to_cart")`

默认倾向：

- `retrieve_strategy = semantic_first`
- `rerank_focus = intent_match`

解释：

- 详情页和加购场景更强调当前商品意图，所以更偏语义召回和意图匹配重排。

### 2. 价格敏感度

当：

- `risk_flags` 包含 `high_price_sensitivity`
- 或 `price_sensitivity >= 0.8`

默认倾向：

- `rerank_focus = price_first`
- `copy_tone = promotion`
- `filters.max_discount_priority = true`

解释：

- 系统会让低价、高性价比、促销型商品更容易排前。

### 3. 流失风险

当：

- `risk_flags` 包含 `high_churn_risk`

默认倾向：

- `risk_policy = retention_boost`
- `copy_tone = reassure`

解释：

- 推荐和文案都会更偏留存、安全感、安抚式表达。

### 4. 首页场景

当：

- `scene = homepage`

默认倾向：

- `filters.diversity_boost = true`

解释：

- 首页更需要多样性，不希望结果过窄。

## 四、条件怎么影响召回

召回主要看 `retrieve_strategy`。

### 1. `semantic_first`

影响因素：

- `recent_views`
- `user_context.short_term.intent_categories`
- `user_context.long_term.preferred_categories_30d`
- `current_item.name`
- `current_item.category`
- `trigger_event`

效果：

- 更偏语义相关商品
- 更适合详情页、强意图场景

### 2. `hybrid`

影响因素：

- 语义召回结果
- 商品库默认排序
- 偏好类目提升

效果：

- 是更稳妥的综合模式
- 兼顾语义相关性和常规商品池覆盖

### 3. `inventory_first`

影响因素：

- `stock`

效果：

- 高库存商品更容易进候选池前列

### 4. `hot_first`

影响因素层级：

1. 热门标签
2. 商品基础分 `score`
3. 库存 `stock`

效果：

- 更偏爆款、热卖、热门商品

### 5. 偏好类目提升

不管前面用了哪种召回策略，如果存在：

- `user_profile.preferred_categories`
- 或 `user_context` 里的偏好类目

系统会再把命中偏好类目的商品往前提。

## 五、条件怎么影响重排

重排主要看 `rerank_focus`。

### 1. `price_first`

影响因素层级：

1. 价格更低优先

适合：

- 高价格敏感用户
- 优惠导向场景

### 2. `brand_first`

影响因素层级：

1. 是否命中偏好品牌
2. 价格更低优先

适合：

- 品牌偏好强的用户

### 3. `diversity`

影响因素层级：

1. 类目分布优先

适合：

- 首页
- 候选商品过于单一时

### 4. `balanced`

效果：

- 规则上不做太强偏置
- 更适合交给 LLM 做综合判断

### 5. `intent_match`

效果：

- 更偏当前意图、当前商品和短期行为匹配

## 六、A/B 条件会影响什么

`ab_config.rerank` 可以直接影响重排方式：

- `rule_based`
- `llm`

解释：

- `rule_based`：走本地确定性规则重排
- `llm`：走 LLM 精排



## 七、Reflection 条件会影响什么

Reflection 是 graph 工作流里的自修正机制。

### 1. 强制触发

当：

- `context.force_reflection = true`

效果：

- 本次请求会强制多走一轮反思修正

### 2. 结果太少

当：

- 最终商品数太少

系统倾向：

- `reflection_hint = too_few_products`
- 把 `retrieve_strategy` 往 `hot_first` 推

### 3. 文案数和商品数不匹配

当：

- `marketing_copies` 数量小于商品数

系统倾向：

- `reflection_hint = copy_count_mismatch`
- 更容易触发 `retention_guard`

### 4. 多样性太差

当：

- 结果类目过于单一

系统倾向：

- `reflection_hint = diversity_tuning`
- 把 `rerank_focus` 往 `diversity` 推

