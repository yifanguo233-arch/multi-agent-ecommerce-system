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


@app.get("/health")
async def health():
    # 简单的就绪/存活检查接口，可用于手动检查或部署探针。
    return {
        "status": "healthy",
        "model": settings.llm_model,
        "redis": redis_status,
        "database": database_status,
        "checkpoint": checkpoint_status,
    }


@app.get("/api/v1/products")
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


@app.get("/api/v1/products/{product_id}")
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


@app.get("/api/v1/products/{product_id}/inventory")
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


@app.post("/api/v1/events")
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
    return {
        "status": "recorded",
        "user_id": event.user_id,
        "behavior_type": event.behavior_type,
        "item_id": event.item_id,
    }


@app.get("/api/v1/users/{user_id}/features")
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


@app.post("/api/v1/users/{user_id}/offline-tags")
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
    return {"status": "merged", "user_id": user_id, "tags": request.tags}


@app.post("/api/v1/recommend", response_model=RecommendationResponse)
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


@app.post("/api/v1/recommend/graph")
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


@app.get("/api/v1/recommend/graph/replay/{thread_id}")
async def replay_graph_state(thread_id: str, history_limit: int = 10):
    """根据 thread_id 从 LangGraph checkpoint 回放图执行状态。"""
    if not rec_graph:
        return {"error": "Graph not initialized"}
    cfg = {"configurable": {"thread_id": thread_id}}
    snapshot = await rec_graph.aget_state(cfg)
    history = []
    count = 0
    async for item in rec_graph.aget_state_history(cfg):
        history.append(
            {
                "next": list(getattr(item, "next", ()) or ()),
                "created_at": str(getattr(item, "created_at", "")),
            }
        )
        count += 1
        if count >= max(1, history_limit):
            break
    values = getattr(snapshot, "values", {}) or {}

    # 即使历史数据里存在旧对象类型，也尽量把 replay 响应整理成 JSON 友好格式。
    final_products = []
    for p in values.get("final_products", []) or []:
        if isinstance(p, dict):
            final_products.append(p)
        elif hasattr(p, "model_dump"):
            final_products.append(p.model_dump(mode="json"))
        else:
            final_products.append({"value": str(p)})

    agent_results = values.get("agent_results", {}) or {}
    if not isinstance(agent_results, dict):
        agent_results = {"value": str(agent_results)}

    return {
        "thread_id": thread_id,
        "request_id": values.get("request_id"),
        "user_id": values.get("user_id"),
        "plan_version": values.get("plan_version", "v1"),
        "plan_payload": values.get("plan_payload", {}),
        "plan_error": values.get("plan_error"),
        "products": final_products,
        "marketing_copies": values.get("marketing_copies", []),
        "agent_results": agent_results,
        "trace_steps": values.get("trace_steps", []),
        "node_latency_ms": values.get("node_latency_ms", {}),
        "errors": values.get("errors", []),
        "history": history,
    }


@app.get("/api/v1/recommend/graph/replay/by-request/{request_id}")
async def replay_graph_state_by_request_id(request_id: str, history_limit: int = 10):
    """通过业务 request_id 查到 thread_id，再回放图执行状态。"""
    thread_id = graph_replay_index.resolve(request_id)
    if not thread_id:
        return {
            "error": "request_id not found in persistent replay index",
            "request_id": request_id,
        }
    return await replay_graph_state(thread_id=thread_id, history_limit=history_limit)


@app.get("/api/v1/experiments")
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


@app.get("/api/v1/metrics")
async def get_metrics():
    """查看系统监控指标。"""
    return {
        "agents": metrics_collector.get_agent_stats(),
        "business": metrics_collector.get_business_stats(),
    }


@app.get("/metrics")
async def get_prometheus_metrics():
    payload, content_type = metrics_collector.prometheus_payload()
    return Response(content=payload, media_type=content_type)


@app.post("/api/v1/experiments/{experiment_id}/outcome")
async def record_outcome(experiment_id: str, group: str, success: bool):
    """记录 A/B 测试结果，并更新 Thompson Sampling 状态。"""
    # 把观测到的实验结果回写给实验引擎，用于后续流量分配。
    ab_engine.record_outcome(experiment_id, group, success)
    return {"status": "recorded"}


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
