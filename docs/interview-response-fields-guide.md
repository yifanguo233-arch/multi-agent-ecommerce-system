# 推荐响应字段解读笔记

这份文档主要记录 `POST /api/v1/recommend/graph` 返回结果里几个关键字段怎么讲。重点分两层：

- `plan_payload`：策略层，说明这次推荐准备怎么做。
- `agent_results`：执行层，说明每个 Agent 实际做了什么。

## plan_payload 是什么

`plan_payload` 可以理解成本次推荐请求的“执行策略单”。它最初由 Planner 生成，后面也可能被 `reflection` 或 tool 节点调整。

## plan_payload 字段

### plan_version

### retrieve_strategy

商品召回策略，决定候选商品怎么来。

可选值：

- `semantic_first`：语义优先。根据 query、用户意图、商品文本做语义召回。
- `hot_first`：热门优先。更偏商品分数、热门标签、爆款商品。
- `inventory_first`：库存优先。更偏库存充足商品。
- `hybrid`：混合召回。语义、热门、规则一起使用。

### rerank_focus

重排重点，决定候选商品怎么排序。

可选值：

- `price_first`：价格优先，适合价格敏感用户。
- `brand_first`：品牌优先，适合有品牌偏好的用户。
- `diversity`：多样性优先，避免推荐结果过窄。
- `balanced`：综合排序。
- `intent_match`：意图匹配优先，适合 query 很明确的请求。

### copy_tone

营销文案语气。

可选值：

- `rational`：理性、事实型，适合高客单价或需要决策成本的商品。
- `promotion`：促销型，强调优惠、折扣、限时。
- `new_release`：新品型，强调上新、新鲜感。
- `reassure`：安抚型，适合流失风险用户或召回场景。
- `default`：默认语气。

### risk_policy

风险策略。

可选值：

- `standard`：标准策略。
- `retention_boost`：流失风险用户，偏召回、安抚和留存。
- `stock_guard`：库存保护策略，目前主要是策略透传。

### business_goal

业务目标，例如：

- `conversion`：促进转化。
- `retention`：促进留存。
- `gmv`：偏成交金额。
- `ctr`：偏点击率。

### filters

策略附加条件。

常见字段：

- `diversity_boost = true`：首页推荐要考虑多样性，避免结果过窄。
- `max_discount_priority = true`：价格敏感用户优先看优惠或高性价比商品。

### metadata

Planner 的过程信息，主要给研发排查。

常见字段：

- `scene`：推荐场景，比如 `homepage`。
- `trigger_event`：触发事件，比如 `browse`。
- `planner_mode`：Planner 模式。
- `llm_parse_ok`：LLM 输出是否解析成功。
- `retry_used`：是否用了修复重试。
- `retry_attempts`：重试次数。
- `raw_response`：LLM 原始返回。

`planner_mode` 常见值：

- `rule_only`：只走规则，没有调用 LLM。
- `rule_llm`：规则先生成合法 plan，再用 LLM refine。
- `rule_fallback`：LLM 超时、异常或解析失败，回退规则 plan。
- `agent_fallback`：Planner Agent 自身异常，返回兜底 plan。

## agent_results 是什么

`agent_results` 是每个 Agent 的执行结果。它说明这次推荐不是固定列表，而是经过多个 Agent 节点处理后得到的。

每个 Agent 通用字段：

- `agent_name`：Agent 名称。
- `success`：是否执行成功。
- `latency_ms`：执行耗时。
- `error`：错误信息，没有错误就是 `null`。
- `data`：调试细节。
- `confidence`：Agent 写入的经验置信值，不是模型自动计算出的概率；通常成功主链路较高，fallback 或失败分支较低。

## 各 Agent 结果

### planner

作用：生成推荐策略。

- `execution_plan`：Planner 生成的策略。
- `data.llm_called`：是否调用 LLM。
- `data.llm_parse_ok`：LLM 输出是否成功解析。
- `data.fallback`：是否走兜底。
- `plan_hit`：是否成功生成计划。

### product_recall

作用：从商品库召回候选商品。

- `data.product_source`：商品来源，当前是数据库。
- `data.catalog_count`：商品库总量。
- `data.candidate_count`：召回了多少候选。
- `data.candidate_product_ids`：候选商品 ID。
- `data.returned_product_ids`：返回给下一步的商品 ID。
- `recall_strategy`：召回策略说明。

### user_profile

作用：生成用户画像。

- `profile.segments`：用户分群， `new_user`、`active`、`high_value`、`price_sensitive`、`churn_risk`。
- `profile.preferred_categories`：偏好类目。
- `profile.price_range`：价格区间。
- `profile.rfm_score`：RFM 分数。`R = Recency（最近度）`，`F = Frequency（频次）`，`M = Monetary（金额）`。规则下有计算公式；走 LLM 画像由 LLM 生成这个结构。
- `profile.real_time_tags`：实时画像标签。
- `data.llm_called`：是否调用 LLM 做画像分析。

### inventory

作用：检查库存和低库存风险。

- `data.total_checked`：检查了多少商品。
- `data.available_count`：有多少商品可售。
- `available_products`：可售商品 ID。
- `low_stock_alerts`：低库存告警。
- `purchase_limits`：限购策略。
- `data.stock_snapshot`：库存快照。

库存告警等级：

- `critical`：库存小于等于 50，建议紧急补货。
- `warning`：库存小于等于 100，建议计划补货。

### rerank

作用：对候选商品重新排序，输出最终商品。

- `data.candidate_count`：重排前候选数量。
- `data.reranked`：LLM 或规则重排后的数量。
- `data.returned_product_ids`：最终排序后的商品 ID。
- `products`：重排后的商品详情。
- `data.llm_called`：是否调用 LLM。
- `data.llm_parse_ok`：LLM 输出是否解析成功。
- `data.fallback`：是否走候选顺序兜底。

### marketing_copy

作用：给最终商品生成营销文案。

- `copies`：最终文案。
- `prompt_template_used`：使用的文案模板。
- `data.llm_called`：是否调用 LLM。
- `data.llm_parse_ok`：是否成功解析 JSON 文案数组。
- `data.fallback`：是否使用规则文案兜底。
- `data.raw_response`：LLM 原始文案输出。

常见模板：

- `new_user`：新用户欢迎和降低决策门槛。
- `high_value`：品质感、尊享感。
- `price_sensitive`：性价比、优惠、折扣。
- `active`：商品亮点和使用场景。
- `churn_risk`：召回、专属折扣、限时紧迫感。

## trace_steps 和 node_latency_ms

### trace_steps

`trace_steps` 记录真实执行过的 LangGraph 节点。

- `init`：初始化请求，做 A/B 分桶。
- `planner`：生成策略。
- `tool_router`：选择是否需要策略工具。
- `tool_executor`：执行策略工具并更新 plan。
- `product_recall`：商品召回。
- `user_profile`：用户画像。
- `merge_phase1`：合并召回和画像。
- `inventory`：库存检查。
- `rerank`：重排。
- `merge_phase2`：合并库存和排序结果。
- `filter`：过滤不可售商品。
- `marketing_copy`：生成文案。
- `reflection`：反思/自修正。
- `aggregate`：聚合最终结果。

### node_latency_ms

`node_latency_ms` 记录每个节点耗时。

## replay 新增排查字段

### replay_meta

重点看：

- `replay_source = langgraph_checkpoint`：说明数据来自 LangGraph checkpoint。
- `is_rerun = false`：说明没有重新调用 LLM 或重新跑推荐。
- `history_count`：读取到多少条 checkpoint 历史。
- `checkpoint_created_at`：checkpoint 创建时间。

### original_request

记录当时原始请求：

- `user_id`
- `scene`
- `num_items`
- `business_goal`
- `context`

### user_context

记录 Planner 前构建的用户上下文快照：

- `short_term`：短期行为。
- `long_term`：长期画像。
- `intent`：当前意图。
- `preference`：汇总偏好。
- `risk_flags`：风险标签。

### initial_plan / final_plan / plan_changes

这三个字段用来看策略变化：

- `initial_plan`：Planner 初始生成的策略。
- `final_plan`：经过 tool/reflection 后最终使用的策略。
- `plan_changes`：哪些字段发生了变化、从什么值变成什么值、原因是什么。

### selected_tools / tool_outputs / reflection_notes

这几个字段用来看链路有没有做自修正：

- `selected_tools`：本轮选中了哪些工具。
- `tool_outputs`：工具具体输出了什么策略变更。
- `reflection_notes`：反思节点为什么触发、触发了几轮。

当前代码里的工具列表：

- `boost_diversity`：把 `rerank_focus` 调整成 `diversity`。
- `retention_guard`：把 `copy_tone` 调整成 `reassure`，把 `risk_policy` 调整成 `retention_boost`。
- `promote_hot`：把 `retrieve_strategy` 调整成 `hot_first`。

### pipeline_products

看商品在链路中的流转：

- `raw_product_ids`：召回阶段得到的商品。
- `available_product_ids`：库存检查后可售的商品。
- `ranked_product_ids`：重排后的商品。
- `final_product_ids`：最终返回给用户的商品。
- `counts`：每个阶段商品数量。

### latency_summary

比 `node_latency_ms` 更适合快速定位慢点：

- `slowest_nodes`：最慢的几个节点。
- `llm_related_latency_ms`：LLM 相关节点总耗时。
- `deterministic_latency_ms`：确定性节点总耗时。

### history

Checkpoint 历史摘要：

- `next`：当时下一个待执行节点；空数组说明图已经跑完。
- `created_at`：checkpoint 时间。
- `metadata`：LangGraph checkpoint 元信息。
- `values_keys`：该 checkpoint 保存了哪些状态字段。
- `trace_count`：当时 trace 有多少步。
- `last_trace_step`：当时最后一个 trace 节点。

