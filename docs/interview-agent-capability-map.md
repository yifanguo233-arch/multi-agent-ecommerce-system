
## 一、整体链路

推荐主链路在 `orchestrator/graph.py` 里定义，核心顺序是：

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
-> reflection / aggregate
```

## 二、LLM 调用点

LLM 调用点只看节点层面，当前主要在这些地方：

- `planner`：修正执行计划。
- `user_profile`：把行为、记忆和请求上下文总结成用户画像。
- `product_recall`：候选召回后可能做一次商品排序，treatment 组会走 LLM rerank。
- `rerank`：对召回候选做最终重排，control 走规则，treatment_llm 走 LLM。
- `marketing_copy`：为最终商品生成营销文案。
- `product_recall` / `rerank` 中的 RAG/embedding 检索：开启 RAG 时会调用 embedding 模型，不是 Chat LLM，但也属于模型调用。

`reflection` 本身不直接调用 LLM；它只是把流程打回前面的节点，所以可能间接导致上述节点再次执行。

### 1. Planner：策略规划

位置：

- `agents/planner_agent.py`
- `services/auto_planner.py`

作用：

先用规则生成基础 `ExecutionPlan`，再让 LLM 做策略 refine。

影响字段：

```json
"retrieve_strategy"
"rerank_focus"
"copy_tone"
"risk_policy"
```

返回里看：

```json
"agent_results.planner.data.llm_called": true,
"planner_mode": "rule_llm",
"llm_parse_ok": true
```

如果 LLM 超时或解析失败，会回退到规则计划：

```json
"planner_mode": "rule_fallback"
```

### 2. User Profile：用户画像

位置：

- `agents/user_profile_agent.py`

作用：

结合短期记忆、长期记忆、请求上下文，用 LLM 提炼用户画像。

返回里看：

```json
"agent_results.user_profile.profile"
"segments"
"preferred_categories"
"price_range"
"real_time_tags"
```

如果 LLM 不可用，会走规则画像兜底。

### 3. Product Recall / Rerank：商品召回与精排

位置：

- `agents/product_rec_agent.py`

注意：

`product_recall` 和 `rerank` 都复用了 `ProductRecAgent`。

作用：

- `product_recall`：从数据库、RAG、规则里召回候选商品
- `rerank`：把候选商品交给 LLM，让 LLM 返回排序后的 `product_id` 列表

返回里看：

```json
"agent_results.product_recall.data.llm_called"
"agent_results.rerank.data.llm_called"
"candidate_product_ids"
"returned_product_ids"
"raw_response"
```

LLM 输出不可解析时，会 retry 修复；仍失败就按候选顺序兜底。

### 4. Marketing Copy：营销文案

位置：

- `agents/marketing_copy_agent.py`

作用：

给最终商品生成文案。

返回里看：

```json
"marketing_copies"
"agent_results.marketing_copy.data.llm_called"
"prompt_template_used"
```

文案 Agent 也有 JSON 解析、retry 修复和 fallback 文案。

## 三、不调 LLM 的节点

这些节点主要是规则、数据库或状态控制：

- `init`：生成 request/thread 信息，做 A/B 分桶
- `tool_router`：根据 plan 或 reflection_hint 选择工具
- `tool_executor`：执行工具并修改 `plan_payload`
- `inventory`：查库存和低库存告警
- `filter`：过滤不可售商品
- `inventory_repair`：没有商品时兜底
- `retention_offer`：高流失风险时加留存模式
- `reflection`：不调 LLM，只改策略并重跑
- `aggregate`：汇总耗时和最终状态

## 四、Reflection 反思机制

位置：

- `orchestrator/graph.py` 的 `route_after_marketing_copy`
- `orchestrator/graph.py` 的 `reflection_node`

### 1. 什么时候触发

触发条件：

```text
force_reflection = true
没有最终商品
文案数量少于商品数量
商品类目太单一
```

强制触发示例：

```json
{
  "user_id": "u_reflection_demo",
  "scene": "homepage",
  "num_items": 3,
  "business_goal": "conversion",
  "context": {
    "query": "游戏高性能笔记本",
    "recent_views": ["笔记本", "游戏", "高性能"],
    "avg_order_amount": 7000,
    "trigger_event": "browse",
    "force_reflection": true
  }
}
```

### 2. 反思会改什么

反思节点会改 `plan_payload`，并把原因写入 `context.reflection_hint`：

```text
too_few_products -> retrieve_strategy = hot_first
copy_count_mismatch -> copy_tone = rational
diversity_tuning -> rerank_focus = diversity
```

然后链路回到：

```text
reflection -> tool_router -> tool_executor -> user_profile + product_recall ...
```

返回里看：

```json
"reflection_notes"
"reflection_count"
"plan_changes"
"trace_steps"
```



## 五、工具路由和策略工具

位置：

- `tool_router_node`
- `tool_executor_node`
- `services/tool_registry.py`

当前注册的工具：

```text
promote_hot
boost_diversity
retention_guard
```

触发逻辑：

```text
retrieve_strategy = hot_first / inventory_first -> promote_hot
rerank_focus = diversity / balanced -> boost_diversity
risk_policy = retention_boost -> retention_guard
reflection_hint = too_few_products -> promote_hot
reflection_hint = diversity_tuning -> boost_diversity
reflection_hint = copy_count_mismatch -> retention_guard
```

返回里看：

```json
"selected_tools"
"tool_outputs"
"plan_changes"
```


## 六、Memory 记忆机制

位置：

- `services/feature_store.py`
- `services/memory_context.py`
- `orchestrator/graph.py` 的 `planner_node`

### 1. 短期记忆

来源：

```text
POST /api/v1/events
```

例子：

```json
{
  "user_id": "u_audio_demo",
  "behavior_type": "view",
  "item_id": "P218",
  "metadata": {
    "category": "耳机",
    "brand": "Sony"
  }
}
```

推荐里体现：

```json
"user_context.short_term.recent_views_1h"
"user_context.short_term.recent_clicks_1h"
"user_context.short_term.active_minutes_30m"
```

### 2. 长期记忆

来源：

```text
POST /api/v1/users/{user_id}/offline-tags
```

例子：

```json
{
  "tags": {
    "preferred_categories_30d": ["耳机"],
    "preferred_brands_30d": ["Sony"],
    "price_sensitivity": 0.9
  }
}
```

推荐里体现：

```json
"user_context.long_term.preferred_categories_30d"
"user_context.long_term.preferred_brands_30d"
"user_context.long_term.price_sensitivity"
"user_context.risk_flags"
```



## 七、RAG / Embedding 召回

位置：

- `agents/product_rec_agent.py`
- `services/rag_vector_search.py`

作用：

商品召回不是只靠关键词硬匹配。`semantic_first` 和 `hybrid` 会尝试语义召回：

```json
"retrieve_strategy": "semantic_first"
```

或：

```json
"retrieve_strategy": "hybrid"
```

实现方式：

- 本地轻量 RAG：词频向量检索
- 可选现代 RAG：Embedding + FAISS/NumPy
- 召回结果再进入 LLM rerank

返回里看：

```json
"candidate_product_ids"
"recall_strategy"
"agent_results.product_recall.data.product_source"
```

注意：

`recall_strategy` 是兼容性描述，不代表每次一定真实执行所有召回方式；具体要结合 `retrieve_strategy` 和候选结果看。

## 八、A/B 实验

位置：

- `services/ab_test.py`
- `orchestrator/graph.py` 的 `init_node`
- `GET /api/v1/experiments`

当前实验：

```text
rec_strategy:
  control -> rule_based
  treatment_llm -> llm

copy_style:
  formal
  casual
```

推荐返回里看：

```json
"experiment_group": "control"
```

或：

```json
"experiment_group": "treatment_llm"
```

outcome 回写：

```text
POST /api/v1/experiments/{experiment_id}/outcome
```

返回里看：

```json
"successes"
"failures"
```


## 九、库存检查与库存兜底

位置：

- `agents/inventory_agent.py`
- `filter_node`
- `inventory_repair_node`

库存 Agent 做：

- 检查候选商品库存
- 过滤不可售商品
- 生成低库存告警
- 设置限购信息

返回里看：

```json
"agent_results.inventory.available_products"
"agent_results.inventory.low_stock_alerts"
"agent_results.inventory.purchase_limits"
"pipeline_products.available_product_ids"
"pipeline_products.final_product_ids"
```

如果过滤后没有商品，会走：

```text
inventory_repair
```

作用：

避免空推荐，用原始召回商品兜底。

## 十、留存保护

位置：

- `services/memory_context.py`
- `route_after_filter`
- `retention_offer_node`

触发来源：

```json
"risk_flags": ["high_churn_risk"]
```

触发后：

```json
"context.retention_mode": true
```

配合策略：

```json
"risk_policy": "retention_boost"
"copy_tone": "reassure"
```



## 十一、Retry、Fallback 与解析修复

位置：

- `agents/base_agent.py`
- `services/auto_planner.py`
- `agents/product_rec_agent.py`
- `agents/user_profile_agent.py`
- `agents/marketing_copy_agent.py`

通用 Agent 层：

```text
BaseAgent.run
-> _retry_execute
-> tenacity retry
-> _fallback
```

LLM 输出层：

- Planner：LLM JSON 解析失败会 retry 修复
- Product rerank：product_id 列表解析失败会 retry 修复
- User profile：画像 JSON 解析失败会 retry 修复
- Marketing copy：文案 JSON 解析失败会 retry 修复

返回里看：

```json
"fallback"
"fallback_reason"
"retry_used"
"retry_attempts"
"retry_succeeded"
"llm_parse_ok"
```


## 十二、Checkpoint / Replay

位置：

- `services/sqlite_checkpoint.py`
- `main.py` 的 replay 接口
- `orchestrator/graph.py` 的 `recommendation_graph_context`

接口：

```text
GET /api/v1/recommend/graph/replay/{thread_id}
GET /api/v1/recommend/graph/replay/by-request/{request_id}
```

作用：

保存 LangGraph 每一步状态，方便回放和解释。

返回里看：

```json
"replay_meta"
"history"
"history[].metadata.step"
"history[].trace_count"
"history[].last_trace_step"
"initial_plan"
"final_plan"
"plan_changes"
"pipeline_products"
```


## 十三、Metrics 与可观测性

位置：

- `services/metrics.py`
- `main.py`

接口：

```text
GET /api/v1/metrics
GET /metrics
```

记录内容：

- Agent 调用次数
- Agent 成功率
- 平均耗时
- 推荐请求总量
- Prometheus 格式指标

返回里看：

```json
"agents"
"business"
"node_latency_ms"
"latency_summary"
```


## 十四、Prompt 模板选择

位置：

- `agents/marketing_copy_agent.py`

作用：

文案 Agent 会根据用户画像和上下文选择模板，比如：

```json
"prompt_template_used": "new_user"
```

常见影响因素：

- 新用户
- 价格敏感
- 留存风险
- 商品标签
- `copy_tone`

返回里看：

```json
"agent_results.marketing_copy.prompt_template_used"
"marketing_copies"
```


