# 持久化与重启恢复说明

本文整理当前系统里哪些数据会长期保存，服务或容器重启后不会恢复默认值，哪些状态只存在于运行时内存里，重启后会丢失。

核验时间：2026-06-14

## 一句话结论

- 会保留：商品库、LangGraph checkpoint / replay 索引、Redis 中尚未过期的用户行为与离线标签、`.env` 等配置文件。
- 不会保留：A/B 实验内存统计、应用 metrics、进程内缓存、临时 user context / user profile / trace 聚合结果。

## 当前环境实际核验结果

当前工作区和运行中的服务已经确认到以下状态：

| 项目 | 当前状态 |
| --- | --- |
| API 健康检查 | 正常 |
| 运行中的主数据库 | Postgres |
| Postgres `products` 表 | 240 条 |
| 本地 `ecommerce.db` | 存在，`products` 表 240 条 |
| 本地 `ecommerce_checkpoints.db` | 存在 |
| checkpoint 表 | 48 条 |
| checkpoint `writes` 表 | 294 条 |
| `graph_replay_index` 表 | 2 条 |
| Redis | 已启用 |
| Redis 当前 key 数 | 7 个 |

说明：

- 当前运行中的 API 通过 `/health` 返回的数据库地址是 Postgres，不是本地 SQLite。
- 工作区里的 `ecommerce.db` 也存在，但它不是当前这次运行实例正在使用的主库。

## 会长期保存的内容

### 1. 商品库数据

当前运行中的 API 使用 Postgres 作为主商品库，`products` 表中的内容不会因为 API 重启而恢复默认值。

为什么会保留：

- `compose.yaml` 给 Postgres 挂了持久化 volume：`postgres_data:/var/lib/postgresql/data`
- API 启动时连接的是 `ECOM_DATABASE_URL=postgresql+psycopg://...@postgres:5432/ecommerce`
- `ProductRepository` 使用数据库读写商品，而不是只放在内存里

当前状态：

- Postgres `products` 表当前有 240 条数据

相关代码：

- [compose.yaml](/c:/my_project/multi-agent-ecommerce-system/compose.yaml:55)
- [main.py](/c:/my_project/multi-agent-ecommerce-system/main.py:380)
- [config/settings.py](/c:/my_project/multi-agent-ecommerce-system/config/settings.py:26)
- [services/product_repository.py](/c:/my_project/multi-agent-ecommerce-system/services/product_repository.py:136)

### 2. LangGraph checkpoint 与 replay 索引

Graph 工作流的执行状态会写入 SQLite checkpoint 文件，重启后仍然可以 replay。

为什么会保留：

- 默认 checkpoint 后端是 SQLite
- `recommendation_graph_context()` 启动时会打开 SQLite checkpointer
- `GraphReplayIndexStore` 会把 `request_id -> thread_id` 写入同一个 SQLite 文件

当前状态：

- [ecommerce_checkpoints.db](/c:/my_project/multi-agent-ecommerce-system/ecommerce_checkpoints.db)
- `checkpoints` 表 48 条
- `writes` 表 294 条
- `graph_replay_index` 表 2 条

这意味着：

- 之前跑过的一部分 graph 请求状态已经被保存
- 可以按 `thread_id` 或 `request_id` 回放历史执行轨迹

相关代码：

- [config/settings.py](/c:/my_project/multi-agent-ecommerce-system/config/settings.py:56)
- [services/sqlite_checkpoint.py](/c:/my_project/multi-agent-ecommerce-system/services/sqlite_checkpoint.py:13)
- [main.py](/c:/my_project/multi-agent-ecommerce-system/main.py:71)
- [main.py](/c:/my_project/multi-agent-ecommerce-system/main.py:672)
- [main.py](/c:/my_project/multi-agent-ecommerce-system/main.py:739)

### 3. Redis 中的用户行为与离线标签

用户行为事件和离线标签会写到 Redis。只要 key 还没过期，Redis 容器重启后这些值仍然存在。

为什么会保留：

- `compose.yaml` 给 Redis 挂了 `redis_data:/data`
- Redis 以 `appendonly yes` 启动，开启了 AOF 持久化
- `FeatureStore.record_behavior()` 会把行为写入 `zset`
- `FeatureStore.merge_offline_tags()` 会把离线标签写入 `string`

当前状态：

- Redis 当前有 7 个 key
- 样例 key：
  - `behavior:11111:purchase`
  - `behavior:u_audio_commute_demo:view`
  - `profile:u_audio_demo`

注意：

- 这部分不是永久保存
- 代码给 key 设置了 TTL，默认 `86400` 秒，也就是 1 天
- 所以它是“跨重启可保留，但会自然过期”的持久化

相关代码：

- [compose.yaml](/c:/my_project/multi-agent-ecommerce-system/compose.yaml:68)
- [config/settings.py](/c:/my_project/multi-agent-ecommerce-system/config/settings.py:18)
- [services/feature_store.py](/c:/my_project/multi-agent-ecommerce-system/services/feature_store.py:29)
- [services/feature_store.py](/c:/my_project/multi-agent-ecommerce-system/services/feature_store.py:114)
- [services/redis_runtime.py](/c:/my_project/multi-agent-ecommerce-system/services/redis_runtime.py:13)
- [main.py](/c:/my_project/multi-agent-ecommerce-system/main.py:530)
- [main.py](/c:/my_project/multi-agent-ecommerce-system/main.py:579)

### 4. 本地 SQLite 商品库文件

工作区里还有一个本地 SQLite 商品库文件 [ecommerce.db](/c:/my_project/multi-agent-ecommerce-system/ecommerce.db)，其中 `products` 表当前也有 240 条数据。

它也属于持久化文件，重启后不会自动恢复默认值，但要区分两件事：

- 它是持久化的
- 它不是当前运行中的 API 主库

也就是说，当前系统“真实在用”的商品主数据以 Postgres 为准，本地 SQLite 更像本地运行或历史阶段留下的数据副本。

## 不会长期保存的内容

### 1. A/B 实验统计

不会保留。

原因：

- `ABTestEngine` 的 `successes`、`failures`、`_metrics` 都在内存对象里
- 没有写入数据库、文件或 Redis

结果：

- 进程重启后，实验统计会重新开始
- 默认实验配置会重新初始化

相关代码：

- [services/ab_test.py](/c:/my_project/multi-agent-ecommerce-system/services/ab_test.py:35)
- [services/ab_test.py](/c:/my_project/multi-agent-ecommerce-system/services/ab_test.py:44)
- [services/ab_test.py](/c:/my_project/multi-agent-ecommerce-system/services/ab_test.py:97)
- [services/ab_test.py](/c:/my_project/multi-agent-ecommerce-system/services/ab_test.py:110)

### 2. 应用 metrics 与业务事件聚合

不会保留。

原因：

- `MetricsCollector` 里的 `_agent_metrics` 和 `_business_events` 都是内存结构
- Prometheus 暴露的是当前进程里的指标，不是本项目自己持久化落盘

结果：

- 服务重启后这些统计清零

相关代码：

- [services/metrics.py](/c:/my_project/multi-agent-ecommerce-system/services/metrics.py:38)
- [services/metrics.py](/c:/my_project/multi-agent-ecommerce-system/services/metrics.py:39)
- [services/metrics.py](/c:/my_project/multi-agent-ecommerce-system/services/metrics.py:64)
- [services/metrics.py](/c:/my_project/multi-agent-ecommerce-system/services/metrics.py:77)

### 3. 进程内商品缓存与 RAG 索引

不会保留。

原因：

- `ProductRecAgent` 的 `_catalog_cache` 是内存缓存
- `rag_index`、`modern_rag` 都是在进程启动后按当前商品目录重建
- 没有把向量索引单独存文件

结果：

- 重启后会重新加载商品并重建检索索引

相关代码：

- [agents/product_rec_agent.py](/c:/my_project/multi-agent-ecommerce-system/agents/product_rec_agent.py:116)
- [agents/product_rec_agent.py](/c:/my_project/multi-agent-ecommerce-system/agents/product_rec_agent.py:118)
- [agents/product_rec_agent.py](/c:/my_project/multi-agent-ecommerce-system/agents/product_rec_agent.py:119)
- [agents/product_rec_agent.py](/c:/my_project/multi-agent-ecommerce-system/agents/product_rec_agent.py:615)

### 4. 临时 user context / user profile / memory snapshot

默认不会长期保留。

原因：

- `MemoryContextEngine.build()` 是按当前请求现场动态拼装 `user_context`
- `UserProfileAgent` 也是基于请求上下文、Redis 特征和 LLM / 规则即时生成画像
- 没有单独的 `users`、`profiles`、`sessions` 持久化表

但有一个例外：

- 如果请求走的是 graph 工作流，那么这些状态会作为 graph state 的一部分进入 checkpoint
- 所以它们可能以“某次请求的执行快照”形式出现在 checkpoint 里
- 但系统里没有一份长期维护的“用户主档画像表”

相关代码：

- [services/memory_context.py](/c:/my_project/multi-agent-ecommerce-system/services/memory_context.py:16)
- [agents/user_profile_agent.py](/c:/my_project/multi-agent-ecommerce-system/agents/user_profile_agent.py:181)
- [orchestrator/graph.py](/c:/my_project/multi-agent-ecommerce-system/orchestrator/graph.py:177)
- [orchestrator/graph.py](/c:/my_project/multi-agent-ecommerce-system/orchestrator/graph.py:194)

### 5. 请求级 trace、节点延迟、一次性调试结果

大部分不会长期保留。

原因：

- 常规接口响应里的 `trace_steps`、`node_latency_ms`、`agent_results` 主要是本次请求的返回内容
- 如果只是普通内存返回，不会自动形成长期数据库记录

例外：

- 走 graph 且开启 checkpoint 时，这些字段可能随 graph state 一起进入 checkpoint 文件

## 当前没有的长期业务存储

从当前代码和运行状态看，系统里没有这些正式持久化对象：

- 没有 `users` 用户主表
- 没有 `orders` 订单表
- 没有独立的长期 `profiles` 用户画像表
- 没有独立的 `sessions` 会话表
- 没有单独落盘的向量库文件
- 没有持久化的 A/B 实验结果库
- 没有持久化的应用级 metrics 历史库

当前真正长期存在的业务主数据，核心还是：

- 商品数据
- Graph checkpoint / replay 映射
- Redis 中尚未过期的行为与标签

## 快速判断表

| 内容 | 存储位置 | 重启后是否保留 | 是否会自动恢复默认值 |
| --- | --- | --- | --- |
| 商品数据（当前主库） | Postgres | 会 | 不会 |
| 本地商品库副本 | `ecommerce.db` | 会 | 不会 |
| Graph checkpoints | `ecommerce_checkpoints.db` | 会 | 不会 |
| replay 索引 | `ecommerce_checkpoints.db` | 会 | 不会 |
| Redis 行为事件 | Redis + AOF + volume | 会 | 不会，但会过期 |
| Redis 离线标签 | Redis + AOF + volume | 会 | 不会，但会过期 |
| A/B 统计 | 进程内存 | 不会 | 会 |
| metrics | 进程内存 | 不会 | 会 |
| 商品缓存 | 进程内存 | 不会 | 会 |
| RAG 索引 | 进程内存 | 不会 | 会 |
| user_context / user_profile | 请求内存 | 默认不会 | 会 |

## 面试表达建议

如果面试里被问“系统有没有长期记忆”，可以直接回答：

> 有，但分层。商品主数据放在数据库里长期保存；LangGraph 的执行状态和 replay 索引保存在 SQLite checkpoint 里；用户实时行为和离线标签放在 Redis 里，能跨重启保留，但带 TTL。A/B 统计、metrics、缓存和大多数 user context 只是进程内状态，重启后会丢。
