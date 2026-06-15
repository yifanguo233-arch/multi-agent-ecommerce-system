"""
Multi-Agent 电商推荐系统的 FastAPI 入口。

主要接口：
  POST /api/v1/recommend          - 获取个性化推荐
  POST /api/v1/recommend/graph    - 通过 LangGraph 工作流获取推荐
  GET  /api/v1/experiments        - 查看 A/B 实验状态
  GET  /api/v1/metrics            - 查看系统监控指标
  GET  /health                    - 健康检查
"""

from __future__ import annotations

import sys
import os
import uuid

# 允许直接用 `python main.py` 启动，同时保留 `from config import get_settings` 这类包式导入。
sys.path.insert(0, os.path.dirname(__file__))

from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from models.schemas import (
    BehaviorEventRequest,
    OfflineTagsRequest,
    RecommendationRequest,
    RecommendationResponse,
)
from orchestrator import graph as graph_runtime
from orchestrator.supervisor import SupervisorOrchestrator
from orchestrator.graph import recommendation_graph_context
from services.ab_test import ABTestEngine
from services.metrics import MetricsCollector
from services.product_repository import get_product_repository
from services.redis_runtime import create_redis_feature_store
from services.sqlite_checkpoint import GraphReplayIndexStore

logger = structlog.get_logger()
settings = get_settings()


# 进程级 singleton 对象，供所有请求共享。
ab_engine = ABTestEngine(bucket_count=settings.ab_test_default_bucket_count)
if not settings.ab_test_enabled:
    for exp in ab_engine.experiments.values():
        exp.enabled = False
metrics_collector = MetricsCollector()
product_repository = get_product_repository(settings.database_url)
supervisor = SupervisorOrchestrator(ab_engine=ab_engine)
rec_graph = None
redis_client = None
redis_feature_store = None
redis_status: dict[str, Any] = {"enabled": False, "error": ""}
database_status: dict[str, Any] = {
    "url": product_repository.safe_database_url,
    "product_count": 0,
    "seeded": 0,
    "error": "",
}
checkpoint_status: dict[str, Any] = {
    "backend": settings.checkpoint_backend,
    "sqlite_path": settings.checkpoint_sqlite_path,
}
graph_replay_index = GraphReplayIndexStore(settings.checkpoint_sqlite_path)


def _remember_graph_request(request_id: str, thread_id: str) -> None:
    graph_replay_index.remember(request_id, thread_id)


async def _invoke_graph_recommendation(request: RecommendationRequest) -> dict[str, Any]:
    if not rec_graph:
        raise RuntimeError("Graph not initialized")
    thread_id = str(uuid.uuid4())
    state = {
        "request_id": thread_id,
        "user_id": request.user_id,
        "scene": request.scene,
        "num_items": request.num_items,
        "business_goal": request.business_goal,
        "context": request.context,
    }
    cfg = {"configurable": {"thread_id": thread_id}}
    result = await rec_graph.ainvoke(state, config=cfg)
    resolved_request_id = str(result.get("request_id", thread_id))
    _remember_graph_request(resolved_request_id, thread_id)
    return {
        "request_id": resolved_request_id,
        "thread_id": thread_id,
        "user_id": result.get("user_id"),
        "products": [
            p if isinstance(p, dict) else p.model_dump(mode="json")
            for p in result.get("final_products", [])
        ],
        "marketing_copies": result.get("marketing_copies", []),
        "experiment_group": result.get("experiment_group", "control"),
        "memory_hit": bool(result.get("memory_hit", False)),
        "plan_version": result.get("plan_version", "v1"),
        "plan_payload": result.get("plan_payload", {}),
        "plan_error": result.get("plan_error"),
        "agent_results": result.get("agent_results", {}),
        "trace_steps": result.get("trace_steps", []),
        "node_latency_ms": result.get("node_latency_ms", {}),
        "errors": result.get("errors", []),
        "total_latency_ms": round(result.get("total_latency_ms", 0), 1),
    }


def _collect_graph_metrics(graph_result: dict[str, Any]) -> None:
    node_latency = graph_result.get("node_latency_ms", {}) or {}
    for node_name, latency in node_latency.items():
        try:
            ms = float(latency)
        except Exception:
            ms = 0.0
        metrics_collector.record_agent_call(
            agent_name=f"graph_node:{node_name}",
            success=True,
            latency_ms=ms,
        )


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "success", "succeeded"}:
            return True
        if normalized in {"false", "0", "no", "n", "fail", "failed", "failure"}:
            return False
    return None


def _resolve_experiment_group(
    user_id: str,
    experiment_id: str,
    metadata: dict[str, Any],
) -> tuple[str, str]:
    metadata_group = metadata.get("experiment_group") or metadata.get("group")
    exp = ab_engine.experiments.get(experiment_id)
    valid_groups = {g.name for g in exp.groups} if exp else set()

    if metadata_group:
        group = str(metadata_group)
        if group in valid_groups:
            return group, "metadata"

    assigned = ab_engine.assign(user_id, experiment_id)
    group = str(assigned.get("group") or "control")
    source = "assignment"
    if metadata_group:
        source = "assignment_fallback_invalid_metadata_group"
    return group, source


def _auto_record_ab_from_event(event: BehaviorEventRequest) -> dict[str, Any]:
    metadata = event.metadata or {}
    experiment_id = str(metadata.get("experiment_id") or "rec_strategy")
    exp = ab_engine.experiments.get(experiment_id)
    if exp is None:
        return {
            "experiment_id": experiment_id,
            "group": "",
            "group_source": "",
            "behavior_type": event.behavior_type,
            "metric_recorded": False,
            "metric_name": "",
            "success_recorded": False,
            "success": None,
            "reason": "unknown_experiment",
        }
    if not exp.enabled:
        return {
            "experiment_id": experiment_id,
            "group": "",
            "group_source": "",
            "behavior_type": event.behavior_type,
            "metric_recorded": False,
            "metric_name": "",
            "success_recorded": False,
            "success": None,
            "reason": "experiment_disabled",
        }

    group, group_source = _resolve_experiment_group(event.user_id, experiment_id, metadata)
    metric_name = str(metadata.get("metric_name") or event.behavior_type)
    ab_engine.record_metric(
        experiment_id=experiment_id,
        group_name=group,
        metric_name=metric_name,
        value=1.0,
        user_id=event.user_id,
    )

    explicit_success = _coerce_optional_bool(
        metadata.get("ab_success", metadata.get("success"))
    )
    success: bool | None = None
    reason = "view_is_impression_only"
    if explicit_success is not None:
        success = explicit_success
        reason = "explicit_success_from_metadata"
    elif event.behavior_type in {"click", "purchase"}:
        success = True
        reason = f"auto_success_from_{event.behavior_type}"

    success_recorded = success is not None
    if success_recorded:
        ab_engine.record_outcome(experiment_id, group, success)

    return {
        "experiment_id": experiment_id,
        "group": group,
        "group_source": group_source,
        "behavior_type": event.behavior_type,
        "metric_recorded": True,
        "metric_name": metric_name,
        "success_recorded": success_recorded,
        "success": success,
        "reason": reason,
    }


def _to_json_product(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return {"value": str(item)}


def _product_ids(items: list[Any] | None) -> list[str]:
    ids: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            pid = item.get("product_id")
        else:
            pid = getattr(item, "product_id", None)
        if pid:
            ids.append(str(pid))
    return ids


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _build_original_request(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": values.get("user_id"),
        "scene": values.get("scene"),
        "num_items": values.get("num_items"),
        "business_goal": values.get("business_goal"),
        "context": values.get("context", {}),
    }


def _planner_initial_plan(agent_results: dict[str, Any]) -> dict[str, Any]:
    planner = _safe_dict(agent_results.get("planner"))
    return _safe_dict(planner.get("execution_plan"))


def _tool_reason_for_field(tool_outputs: dict[str, Any], field: str) -> str:
    for tool_name, output in tool_outputs.items():
        result = _safe_dict(_safe_dict(output).get("result"))
        if field in result:
            return str(result.get("reason") or tool_name)
    return ""


def _build_plan_changes(
    initial_plan: dict[str, Any],
    final_plan: dict[str, Any],
    tool_outputs: dict[str, Any],
) -> list[dict[str, Any]]:
    fields = ["retrieve_strategy", "rerank_focus", "copy_tone", "risk_policy", "business_goal"]
    changes: list[dict[str, Any]] = []
    for field in fields:
        before = initial_plan.get(field)
        after = final_plan.get(field)
        if before == after:
            continue
        changes.append(
            {
                "field": field,
                "from": before,
                "to": after,
                "reason": _tool_reason_for_field(tool_outputs, field) or final_plan.get("reason", ""),
            }
        )
    return changes


def _build_pipeline_products(values: dict[str, Any]) -> dict[str, Any]:
    raw_products = _safe_list(values.get("raw_products"))
    ranked_products = _safe_list(values.get("ranked_products"))
    final_products = _safe_list(values.get("final_products"))
    available_ids = _safe_list(values.get("available_ids"))
    return {
        "raw_product_ids": _product_ids(raw_products),
        "available_product_ids": [str(x) for x in available_ids],
        "ranked_product_ids": _product_ids(ranked_products),
        "final_product_ids": _product_ids(final_products),
        "counts": {
            "raw": len(raw_products),
            "available": len(available_ids),
            "ranked": len(ranked_products),
            "final": len(final_products),
        },
    }


def _build_latency_summary(node_latency_ms: dict[str, Any]) -> dict[str, Any]:
    latencies: dict[str, float] = {}
    for node, value in node_latency_ms.items():
        try:
            latencies[str(node)] = float(value)
        except Exception:
            latencies[str(node)] = 0.0

    llm_nodes = {"planner", "product_recall", "user_profile", "rerank", "marketing_copy"}
    llm_related = sum(ms for node, ms in latencies.items() if node in llm_nodes)
    deterministic = sum(ms for node, ms in latencies.items() if node not in llm_nodes)
    slowest = sorted(latencies.items(), key=lambda item: item[1], reverse=True)[:5]
    return {
        "slowest_nodes": [{"node": node, "latency_ms": round(ms, 3)} for node, ms in slowest],
        "llm_related_latency_ms": round(llm_related, 3),
        "deterministic_latency_ms": round(deterministic, 3),
    }


def _last_trace_step(trace_steps: list[Any]) -> dict[str, Any] | None:
    for item in reversed(trace_steps):
        if isinstance(item, dict):
            return item
    return None


def _history_item_summary(item: Any) -> dict[str, Any]:
    values = _safe_dict(getattr(item, "values", {}) or {})
    trace_steps = _safe_list(values.get("trace_steps"))
    metadata = _safe_dict(getattr(item, "metadata", {}) or {})
    return {
        "next": list(getattr(item, "next", ()) or ()),
        "created_at": str(getattr(item, "created_at", "")),
        "metadata": metadata,
        "values_keys": sorted(str(k) for k in values.keys()),
        "trace_count": len(trace_steps),
        "last_trace_step": _last_trace_step(trace_steps),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时构建一次 LangGraph workflow，后续请求复用同一个实例。
    global rec_graph, redis_client, redis_feature_store, redis_status, database_status
    try:
        seeded = (
            product_repository.seed_if_empty(settings.product_seed_count)
            if settings.product_auto_seed
            else 0
        )
        database_status = {
            "url": product_repository.safe_database_url,
            "product_count": product_repository.count_products(),
            "seeded": seeded,
            "error": "",
        }
    except Exception as exc:
        database_status = {
            "url": product_repository.safe_database_url,
            "product_count": 0,
            "seeded": 0,
            "error": str(exc),
        }
        logger.warning("app.database_unavailable", error=str(exc))
    redis_client, redis_feature_store, redis_error = await create_redis_feature_store(settings)
    redis_status = {"enabled": redis_feature_store is not None, "error": redis_error}
    supervisor.configure_feature_store(redis_feature_store)
    graph_runtime.configure_feature_store(redis_feature_store)
    try:
        async with recommendation_graph_context() as graph:
            rec_graph = graph
            logger.info(
                "app.startup",
                model=settings.llm_model,
                redis_enabled=redis_status["enabled"],
                checkpoint_backend=settings.checkpoint_backend,
                checkpoint_sqlite_path=settings.checkpoint_sqlite_path,
            )
            yield
    finally:
        rec_graph = None
        if redis_client is not None:
            await redis_client.aclose()
        logger.info(
            "app.shutdown",
            checkpoint_backend=settings.checkpoint_backend,
            checkpoint_sqlite_path=settings.checkpoint_sqlite_path,
        )


app = FastAPI(
    title="Multi-Agent E-Commerce Recommendation System",
    description="用户画像Agent + 商品推荐Agent + 营销文案Agent + 库存决策Agent，并行+聚合模式",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.get(
    "/health",
    operation_id="get_health",
    responses={200: {"description": "Service health and dependency status."}},
)
async def health():
    # 简单的就绪/存活检查接口，可用于手动检查或部署探针。
    return {
        "status": "healthy",
        "model": settings.llm_model,
        "redis": redis_status,
        "database": database_status,
        "checkpoint": checkpoint_status,
    }


@app.get(
    "/api/v1/products",
    operation_id="list_products",
    responses={200: {"description": "Catalog product list."}},
)
async def list_products(
    limit: int = 20,
    offset: int = 0,
    category: str | None = None,
    in_stock_only: bool = False,
):
    """查看数据库商品目录，方便演示和追踪推荐来源。"""
    products = product_repository.list_products(
        limit=limit,
        offset=offset,
        category=category,
        in_stock_only=in_stock_only,
    )
    return {
        "source": "database",
        "database_url": product_repository.safe_database_url,
        "total_count": product_repository.count_products(),
        "limit": max(1, min(500, int(limit))),
        "offset": max(0, int(offset)),
        "category": category,
        "in_stock_only": in_stock_only,
        "products": [p.model_dump(mode="json") for p in products],
    }


@app.get(
    "/api/v1/products/{product_id}",
    operation_id="get_product",
    responses={200: {"description": "One product detail."}},
)
async def get_product(product_id: str):
    """根据推荐商品 ID 返回数据库中的商品记录。"""
    product = product_repository.get_product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail={"message": "product not found", "product_id": product_id})
    return {
        "source": "database",
        "database_url": product_repository.safe_database_url,
        "product": product.model_dump(mode="json"),
    }


@app.get(
    "/api/v1/products/{product_id}/inventory",
    operation_id="get_product_inventory",
    responses={200: {"description": "One product inventory snapshot."}},
)
async def get_product_inventory(product_id: str):
    """从商品表返回当前库存。"""
    product = product_repository.get_product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail={"message": "product not found", "product_id": product_id})
    return {
        "source": "database",
        "database_url": product_repository.safe_database_url,
        "product_id": product.product_id,
        "name": product.name,
        "stock": product.stock,
        "available": product.stock > 0,
    }


@app.post(
    "/api/v1/events",
    operation_id="record_behavior_event",
    responses={200: {"description": "Behavior event recorded into the feature store."}},
)
async def record_behavior_event(event: BehaviorEventRequest):
    """把实时用户行为事件写入 Redis 特征存储。"""
    if redis_feature_store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Redis FeatureStore is unavailable",
                "redis": redis_status,
            },
        )
    await redis_feature_store.record_behavior(
        user_id=event.user_id,
        behavior_type=event.behavior_type,
        item_id=event.item_id,
        metadata=event.metadata,
    )
    ab_outcome = _auto_record_ab_from_event(event)
    return {
        "status": "recorded",
        "user_id": event.user_id,
        "behavior_type": event.behavior_type,
        "item_id": event.item_id,
        "ab_outcome": ab_outcome,
    }


@app.get(
    "/api/v1/users/{user_id}/features",
    operation_id="get_user_features",
    responses={200: {"description": "User features from Redis."}},
)
async def get_user_features(user_id: str):
    """查看 Redis 中的实时用户特征，方便演示和调试。"""
    if redis_feature_store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Redis FeatureStore is unavailable",
                "redis": redis_status,
            },
        )
    return await redis_feature_store.get_user_features(user_id)


@app.post(
    "/api/v1/users/{user_id}/offline-tags",
    operation_id="merge_offline_tags",
    responses={200: {"description": "Offline tags merged into the feature store."}},
)
async def merge_offline_tags(user_id: str, request: OfflineTagsRequest):
    """把离线画像标签合并到 Redis，供推荐链路使用。"""
    if redis_feature_store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Redis FeatureStore is unavailable",
                "redis": redis_status,
            },
        )
    await redis_feature_store.merge_offline_tags(user_id=user_id, tags=request.tags)
    return {
        "status": "merged",
        "user_id": user_id,
        "tags": request.tags,
    }


@app.post(
    "/api/v1/recommend",
    response_model=RecommendationResponse,
    operation_id="recommend",
    responses={200: {"description": "Recommendation response."}},
)
async def recommend(request: RecommendationRequest):
    """统一推荐入口：优先走 LangGraph workflow，失败时自动降级到 Supervisor。"""
    mode = (settings.orchestration_mode or "graph").strip().lower()
    if mode == "graph":
        try:
            g = await _invoke_graph_recommendation(request)
            _collect_graph_metrics(g)
            metrics_collector.record_request(
                entrypoint="recommend",
                mode="graph",
                success=True,
                latency_ms=float(g.get("total_latency_ms", 0.0)),
            )
            return RecommendationResponse(
                request_id=str(g.get("request_id", "")),
                user_id=str(g.get("user_id", request.user_id)),
                products=g.get("products", []),
                marketing_copies=g.get("marketing_copies", []),
                experiment_group=str(g.get("experiment_group", "control")),
                memory_hit=bool(g.get("memory_hit", False)),
                plan_version=str(g.get("plan_version", "v1")),
                plan_hit=True,
                execution_plan=g.get("plan_payload", {}),
                plan_payload=g.get("plan_payload", {}),
                debug_context={
                    "graph_thread_id": g.get("thread_id"),
                    "agent_results": g.get("agent_results", {}),
                    "trace_steps": g.get("trace_steps", []),
                    "node_latency_ms": g.get("node_latency_ms", {}),
                    "graph_errors": g.get("errors", []),
                },
                total_latency_ms=float(g.get("total_latency_ms", 0.0)),
            )
        except Exception as exc:
            logger.warning("recommend.graph_fallback_supervisor", error=str(exc))
            response = await supervisor.recommend(request)
            _collect_metrics(response)
            metrics_collector.record_request(
                entrypoint="recommend",
                mode="graph_fallback_supervisor",
                success=True,
                latency_ms=float(response.total_latency_ms),
            )
            return response

    response = await supervisor.recommend(request)
    _collect_metrics(response)
    metrics_collector.record_request(
        entrypoint="recommend",
        mode="supervisor",
        success=True,
        latency_ms=float(response.total_latency_ms),
    )
    return response


@app.post(
    "/api/v1/recommend/graph",
    operation_id="recommend_via_graph",
    responses={200: {"description": "LangGraph recommendation response with replay ids and trace data."}},
)
async def recommend_via_graph(request: RecommendationRequest):
    """使用 LangGraph state graph 执行推荐，主要用于展示 graph workflow 能力。"""
    g = await _invoke_graph_recommendation(request)
    _collect_graph_metrics(g)
    metrics_collector.record_request(
        entrypoint="recommend_graph",
        mode="graph",
        success=True,
        latency_ms=float(g.get("total_latency_ms", 0.0)),
    )
    return g


@app.get(
    "/api/v1/recommend/graph/replay/{thread_id}",
    operation_id="replay_graph_state",
    responses={200: {"description": "Saved LangGraph checkpoint state by thread_id."}},
)
async def replay_graph_state(thread_id: str, history_limit: int = 10):
    """根据 thread_id 从 LangGraph checkpoint 回放图执行状态。"""
    if not rec_graph:
        return {"error": "Graph not initialized"}
    cfg = {"configurable": {"thread_id": thread_id}}
    snapshot = await rec_graph.aget_state(cfg)
    history = []
    count = 0
    async for item in rec_graph.aget_state_history(cfg):
        history.append(_history_item_summary(item))
        count += 1
        if count >= max(1, history_limit):
            break
    values = getattr(snapshot, "values", {}) or {}

    # 即使历史数据里存在旧对象类型，也尽量把 replay 响应整理成 JSON 友好格式。
    final_products = [_to_json_product(p) for p in values.get("final_products", []) or []]

    agent_results = values.get("agent_results", {}) or {}
    if not isinstance(agent_results, dict):
        agent_results = {"value": str(agent_results)}
    tool_outputs = _safe_dict(values.get("tool_outputs"))
    initial_plan = _planner_initial_plan(agent_results)
    final_plan = _safe_dict(values.get("plan_payload"))
    node_latency_ms = _safe_dict(values.get("node_latency_ms"))
    checkpoint_created_at = history[0]["created_at"] if history else ""

    return {
        "thread_id": thread_id,
        "request_id": values.get("request_id"),
        "replay_meta": {
            "replay_source": "langgraph_checkpoint",
            "is_rerun": False,
            "history_limit": history_limit,
            "history_count": len(history),
            "checkpoint_created_at": checkpoint_created_at,
        },
        "original_request": _build_original_request(values),
        "user_id": values.get("user_id"),
        "user_context": values.get("user_context", {}),
        "plan_version": values.get("plan_version", "v1"),
        "initial_plan": initial_plan,
        "final_plan": final_plan,
        "plan_changes": _build_plan_changes(initial_plan, final_plan, tool_outputs),
        "plan_payload": values.get("plan_payload", {}),
        "plan_error": values.get("plan_error"),
        "selected_tools": values.get("selected_tools", []),
        "tool_outputs": tool_outputs,
        "reflection_notes": values.get("reflection_notes", []),
        "pipeline_products": _build_pipeline_products(values),
        "products": final_products,
        "marketing_copies": values.get("marketing_copies", []),
        "agent_results": agent_results,
        "trace_steps": values.get("trace_steps", []),
        "node_latency_ms": node_latency_ms,
        "latency_summary": _build_latency_summary(node_latency_ms),
        "errors": values.get("errors", []),
        "total_latency_ms": round(values.get("total_latency_ms", 0), 1),
        "history": history,
    }


@app.get(
    "/api/v1/recommend/graph/replay/by-request/{request_id}",
    operation_id="replay_graph_state_by_request_id",
    responses={200: {"description": "Saved LangGraph checkpoint state resolved by request_id."}},
)
async def replay_graph_state_by_request_id(request_id: str, history_limit: int = 10):
    """通过业务 request_id 查到 thread_id，再回放图执行状态。"""
    thread_id = graph_replay_index.resolve(request_id)
    if not thread_id:
        return {
            "error": "request_id not found in persistent replay index",
            "request_id": request_id,
        }
    return await replay_graph_state(thread_id=thread_id, history_limit=history_limit)


@app.get(
    "/api/v1/experiments",
    operation_id="get_experiments",
    responses={200: {"description": "A/B experiment configuration and current stats."}},
)
async def get_experiments():
    """查看所有 A/B 实验状态。"""
    experiments = {}
    # 把内部实验对象转换成 JSON 友好的字典。
    for exp_id, exp in ab_engine.experiments.items():
        experiments[exp_id] = {
            "name": exp.name,
            "enabled": exp.enabled,
            "groups": [
                {
                    "name": g.name,
                    "weight": g.weight,
                    "config": g.config,
                    "successes": g.successes,
                    "failures": g.failures,
                }
                for g in exp.groups
            ],
            "stats": ab_engine.get_stats(exp_id),
        }
    return experiments


@app.get(
    "/api/v1/metrics",
    operation_id="get_application_metrics",
    responses={200: {"description": "JSON metrics grouped by agent and business dimensions."}},
)
async def get_metrics():
    """查看系统监控指标。"""
    return {
        "agents": metrics_collector.get_agent_stats(),
        "business": metrics_collector.get_business_stats(),
    }


@app.get(
    "/metrics",
    operation_id="get_prometheus_metrics",
    responses={200: {"description": "Prometheus text exposition format for scraping."}},
)
async def get_prometheus_metrics():
    payload, content_type = metrics_collector.prometheus_payload()
    related = (
        "# related_endpoint application_metrics GET /api/v1/metrics\n"
        "# related_endpoint experiments GET /api/v1/experiments\n"
    )
    return Response(content=related.encode("utf-8") + payload, media_type=content_type)


@app.post(
    "/api/v1/experiments/{experiment_id}/outcome",
    operation_id="record_experiment_outcome",
    responses={200: {"description": "A/B experiment outcome recorded."}},
)
async def record_outcome(experiment_id: str, group: str, success: bool):
    """记录 A/B 测试结果，并更新 Thompson Sampling 状态。"""
    # 把观测到的实验结果回写给实验引擎，用于后续流量分配。
    ab_engine.record_outcome(experiment_id, group, success)
    return {
        "status": "recorded",
        "experiment_id": experiment_id,
        "group": group,
        "success": success,
    }


def _collect_metrics(response: RecommendationResponse):
    # 从编排响应中记录每个 Agent 的成功率和延迟。
    for name, result in response.agent_results.items():
        metrics_collector.record_agent_call(
            agent_name=name,
            success=result.success,
            latency_ms=result.latency_ms,
        )


if __name__ == "__main__":
    # 本地开发入口。
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
