"""
多 Agent 推荐 pipeline 的 LangGraph state graph。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from operator import add
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, StateGraph

from agents import (
    InventoryAgent,
    MarketingCopyAgent,
    PlannerAgent,
    ProductRecAgent,
    UserProfileAgent,
)
from config import get_settings
from models.schemas import ExecutionPlan, Product, UserProfile
from services.ab_test import ABTestEngine
from services.feature_store import FeatureStore
from services.memory_context import MemoryContextEngine
from services.sqlite_checkpoint import sqlite_checkpointer
from services.tool_registry import ToolRegistry


def _merge_dict(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a or {})
    out.update(b or {})
    return out


class PipelineState(TypedDict, total=False):
    # 一次请求会作为共享 state 在 graph 中流转；每个 node 只返回自己改动的字段，
    # 再由 LangGraph 合并这些局部更新。
    request_id: str
    user_id: str
    scene: str
    num_items: int
    business_goal: str
    context: dict[str, Any]
    experiment_group: str
    experiment_config: dict[str, Any]
    memory_hit: bool

    user_context: dict[str, Any]
    user_profile: dict[str, Any] | None
    raw_products: list[dict[str, Any]]
    ranked_products: list[dict[str, Any]]
    available_ids: list[str]
    final_products: list[dict[str, Any]]
    marketing_copies: list[dict[str, str]]

    # Planner 会把结构化 plan 写到这里，下游 node 可以直接做确定性决策，
    # 不需要解析自由文本形式的 LLM 输出。
    execution_plan: dict[str, Any] | None
    plan_version: str
    plan_payload: dict[str, Any]
    plan_error: str | None
    reflection_count: int
    max_reflections: int
    # 这些字段会被多个 node 写入，因此使用 reducer，而不是最后一次写入覆盖。
    reflection_notes: Annotated[list[dict[str, Any]], add]
    selected_tools: list[str]
    tool_outputs: Annotated[dict[str, Any], _merge_dict]

    agent_results: Annotated[dict[str, Any], _merge_dict]
    trace_steps: Annotated[list[dict[str, Any]], add]
    node_latency_ms: Annotated[dict[str, float], _merge_dict]
    errors: Annotated[list[dict[str, Any]], add]
    total_latency_ms: float
    _start_time: float


user_profile_agent = UserProfileAgent()
product_rec_agent = ProductRecAgent()
marketing_copy_agent = MarketingCopyAgent()
inventory_agent = InventoryAgent()
planner_agent = PlannerAgent()
ab_engine = ABTestEngine()
memory_engine = MemoryContextEngine(getattr(user_profile_agent, "feature_store", None))
tool_registry = ToolRegistry()


def configure_feature_store(feature_store: FeatureStore | None) -> None:
    """把运行时 FeatureStore 注入到 graph 级 singleton Agent 中。"""
    global memory_engine
    # Redis 是可选依赖；可用时，画像生成和 memory context 会共用同一份实时特征源。
    user_profile_agent.feature_store = feature_store
    memory_engine = MemoryContextEngine(feature_store)


def _tool_promote_hot(payload: dict[str, Any]) -> dict[str, Any]:
    return {"retrieve_strategy": "hot_first", "reason": "promote_hot"}


def _tool_boost_diversity(payload: dict[str, Any]) -> dict[str, Any]:
    return {"rerank_focus": "diversity", "reason": "boost_diversity"}


def _tool_retention_guard(payload: dict[str, Any]) -> dict[str, Any]:
    return {"copy_tone": "reassure", "risk_policy": "retention_boost", "reason": "retention_guard"}


tool_registry.register("promote_hot", _tool_promote_hot, "Prefer hot items in recall")  # tool 分类：路由判断见 orchestrator/graph.py:212、222、223
tool_registry.register("boost_diversity", _tool_boost_diversity, "Increase category diversity")  # tool 分类：路由判断见 orchestrator/graph.py:212、224、225
tool_registry.register("retention_guard", _tool_retention_guard, "Retention-safe copy/risk policy")  # tool 分类：路由判断见 orchestrator/graph.py:212、226、227


def _node_meta(name: str, start: float) -> tuple[list[dict[str, Any]], dict[str, float]]:
    return ([{"node": name}], {name: (time.perf_counter() - start) * 1000})


def _plan_dict(state: PipelineState) -> dict[str, Any]:
    payload = state.get("plan_payload", {})
    if isinstance(payload, dict) and payload:
        return payload
    plan = state.get("execution_plan")
    return plan if isinstance(plan, dict) else {}


def _agent_context(state: PipelineState) -> dict[str, Any]:
    return {
        **state.get("context", {}),
        "user_context": state.get("user_context", {}),
        "execution_plan": _plan_dict(state),
        "experiment_group": state.get("experiment_group", "control"),
        "ab_config": state.get("experiment_config", {}),
    }


def _to_products(items: list[dict[str, Any]]) -> list[Product]:
    out: list[Product] = []
    for item in items:
        if isinstance(item, dict):
            out.append(Product.model_validate(item))
    return out


def _to_product_dicts(items: list[Product]) -> list[dict[str, Any]]:
    return [p.model_dump(mode="json") for p in items]


def _to_user_profile(data: dict[str, Any] | None) -> UserProfile | None:
    if not isinstance(data, dict):
        return None
    return UserProfile.model_validate(data)


async def init_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    exp = ab_engine.assign(state["user_id"])
    trace, latency = _node_meta("init", start)
    return {
        "request_id": state.get("request_id", str(uuid.uuid4())),
        "_start_time": start,
        "context": state.get("context", {}),
        "business_goal": state.get("business_goal", "conversion"),
        "experiment_group": exp.get("group", "control"),
        "experiment_config": exp.get("config", {}),
        "agent_results": {},
        "trace_steps": trace,
        "node_latency_ms": latency,
        "errors": [],
    }


async def planner_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    try:
        # 先构建 user context，再让 Planner 根据短期意图、长期偏好和风险标签生成 plan。
        user_context = await memory_engine.build(
            user_id=state["user_id"],
            scene=state["scene"],
            context=state.get("context", {}),
        )
        ctx = user_context.model_dump(mode="json")
        planner_result = await planner_agent.run(
            user_id=state["user_id"],
            scene=state["scene"],
            user_context=ctx,
            request_context=state.get("context", {}),
            business_goal=state.get("business_goal", "conversion"),
        )
        plan = planner_result.execution_plan
        trace, latency = _node_meta("planner", start)
        trace[0]["plan_version"] = plan.plan_version
        return {
            "user_context": ctx,
            "memory_hit": getattr(memory_engine, "feature_store", None) is not None,
            "execution_plan": plan.model_dump(mode="json"),
            "plan_version": plan.plan_version,
            "plan_payload": plan.model_dump(mode="json"),
            "plan_error": None,
            "reflection_count": state.get("reflection_count", 0),
            "max_reflections": state.get("max_reflections", 1),
            "reflection_notes": [],
            "selected_tools": [],
            "tool_outputs": {},
            "agent_results": {"planner": planner_result.model_dump(mode="json")},
            "trace_steps": trace,
            "node_latency_ms": latency,
        }
    except Exception as exc:
        default_plan = ExecutionPlan.default()
        trace, latency = _node_meta("planner", start)
        trace[0]["plan_version"] = default_plan.plan_version
        return {
            "execution_plan": default_plan.model_dump(mode="json"),
            "plan_version": default_plan.plan_version,
            "plan_payload": default_plan.model_dump(mode="json"),
            "plan_error": str(exc),
            "memory_hit": False,
            "selected_tools": [],
            "tool_outputs": {},
            "trace_steps": trace,
            "node_latency_ms": latency,
            "errors": [{"node": "planner", "error": str(exc)}],
        }


async def tool_router_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    selected: list[str] = []
    # Router 只负责选择 strategy tool，不直接修改 plan；executor 会统一应用 tool 输出。
    plan = dict(state.get("plan_payload", {}))
    retrieve_strategy = str(plan.get("retrieve_strategy", "hybrid"))
    rerank_focus = str(plan.get("rerank_focus", "balanced"))
    risk_policy = str(plan.get("risk_policy", "standard"))
    reflection_hint = str(state.get("context", {}).get("reflection_hint", ""))

    if retrieve_strategy in {"hot_first", "inventory_first"} or reflection_hint == "too_few_products":
        selected.append("promote_hot")
    if rerank_focus in {"diversity", "balanced"} or reflection_hint == "diversity_tuning":
        selected.append("boost_diversity")
    if risk_policy == "retention_boost" or reflection_hint == "copy_count_mismatch":
        selected.append("retention_guard")

    trace, latency = _node_meta("tool_router", start)
    trace[0]["selected_tools"] = selected
    return {
        "selected_tools": selected,
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def tool_executor_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    selected = state.get("selected_tools", [])
    outputs: dict[str, Any] = {}
    updated_plan = dict(state.get("plan_payload", {}))
    # Tool 结果会合并回 plan_payload，后续 Agent 只需要消费同一个策略对象。
    for tool_name in selected:
        out = tool_registry.run(tool_name, {"state": state, "plan": updated_plan})
        outputs[tool_name] = out
        if out.get("ok") and isinstance(out.get("result"), dict):
            updated_plan.update(out["result"])

    trace, latency = _node_meta("tool_executor", start)
    trace[0]["tool_count"] = len(selected)
    return {
        "tool_outputs": outputs,
        "plan_payload": updated_plan,
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def user_profile_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    merged_context = _agent_context(state)
    result = await user_profile_agent.run(user_id=state["user_id"], context=merged_context)
    trace, latency = _node_meta("user_profile", start)
    return {
        "user_profile": (
            result.profile.model_dump(mode="json")
            if getattr(result, "profile", None) is not None
            else None
        ),
        "agent_results": {"user_profile": result.model_dump(mode="json")},
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def product_recall_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    # 初始 recall 不强依赖 user profile，先使用 request context、memory context 和 execution plan。
    merged_context = _agent_context(state)
    result = await product_rec_agent.run(
        user_profile=None,
        context=merged_context,
        num_items=state.get("num_items", 10) * 2,
    )
    raw_products = _to_product_dicts(getattr(result, "products", []))
    trace, latency = _node_meta("product_recall", start)
    trace[0]["output_count"] = len(raw_products)
    return {
        "raw_products": raw_products,
        "agent_results": {"product_recall": result.model_dump(mode="json")},
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def merge_phase1_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    trace, latency = _node_meta("merge_phase1", start)
    return {"trace_steps": trace, "node_latency_ms": latency}


async def rerank_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    merged_context = _agent_context(state)
    candidates = _to_products(state.get("raw_products", []))
    result = await product_rec_agent.run(
        user_profile=_to_user_profile(state.get("user_profile")),
        context=merged_context,
        candidates=candidates,
        num_items=state.get("num_items", 10),
    )
    ranked = _to_product_dicts(getattr(result, "products", candidates))
    trace, latency = _node_meta("rerank", start)
    trace[0]["input_count"] = len(candidates)
    trace[0]["output_count"] = len(ranked)
    return {
        "ranked_products": ranked,
        "agent_results": {"rerank": result.model_dump(mode="json")},
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def inventory_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    merged_context = _agent_context(state)
    products = _to_products(state.get("raw_products", []))
    result = await inventory_agent.run(products=products, context=merged_context)
    available_ids = list(getattr(result, "available_products", []))
    trace, latency = _node_meta("inventory", start)
    trace[0]["input_count"] = len(products)
    trace[0]["output_count"] = len(available_ids)
    return {
        "available_ids": available_ids,
        "agent_results": {"inventory": result.model_dump(mode="json")},
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def merge_phase2_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    trace, latency = _node_meta("merge_phase2", start)
    return {"trace_steps": trace, "node_latency_ms": latency}


async def filter_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    ranked = state.get("ranked_products", [])
    avail = set(state.get("available_ids", []))
    num = state.get("num_items", 10)
    final = [p for p in ranked if str(p.get("product_id", "")) in avail]
    if not final:
        final = ranked
    final_products = final[:num]
    trace, latency = _node_meta("filter", start)
    trace[0]["input_count"] = len(ranked)
    trace[0]["output_count"] = len(final_products)
    return {
        "final_products": final_products,
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


def route_after_filter(
    state: PipelineState,
) -> Literal["inventory_repair", "retention_offer", "marketing_copy"]:
    # 即使严格库存过滤移除了所有 ranked products，也要尽量返回可用结果。
    if not state.get("final_products"):
        return "inventory_repair"
    user_context = state.get("user_context", {})
    risk_flags = user_context.get("risk_flags", []) if isinstance(user_context, dict) else []
    # Retention 处理是业务分支，不是单独的推荐模型；它会在生成 copy 前标记 context。
    if "high_churn_risk" in risk_flags:
        return "retention_offer"
    return "marketing_copy"


async def inventory_repair_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    final_products = state.get("raw_products", [])[: state.get("num_items", 10)]
    trace, latency = _node_meta("inventory_repair", start)
    trace[0]["output_count"] = len(final_products)
    return {
        "final_products": final_products,
        "agent_results": {"fallback_recall": {"used_hot_products": True}},
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def retention_offer_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    context = dict(state.get("context", {}))
    context["retention_mode"] = True
    trace, latency = _node_meta("retention_offer", start)
    return {"context": context, "trace_steps": trace, "node_latency_ms": latency}


async def marketing_copy_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    merged_context = _agent_context(state)
    products = _to_products(state.get("final_products", []))
    result = await marketing_copy_agent.run(
        user_profile=_to_user_profile(state.get("user_profile")),
        products=products,
        context=merged_context,
    )
    copies = getattr(result, "copies", [])
    trace, latency = _node_meta("marketing_copy", start)
    trace[0]["input_count"] = len(products)
    trace[0]["output_count"] = len(copies)
    return {
        "marketing_copies": copies,
        "agent_results": {"marketing_copy": result.model_dump(mode="json")},
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def reflection_node(state: PipelineState) -> PipelineState:
    """Self-correction loop：调整 strategy hint，并重试下游 node。"""
    start = time.perf_counter()
    reflection_count = int(state.get("reflection_count", 0)) + 1
    context = dict(state.get("context", {}))
    plan_payload = dict(state.get("plan_payload", {}))

    # Reflection 会先调整策略再重试；如果用同一个 plan 重跑同一路径，
    # 通常只相当于普通 retry，很难改善推荐质量。
    reason = "insufficient_result_quality"
    if len(state.get("final_products", [])) < max(1, state.get("num_items", 10) // 2):
        reason = "too_few_products"
        plan_payload["retrieve_strategy"] = "hot_first"
    elif len(state.get("marketing_copies", [])) < len(state.get("final_products", [])):
        reason = "copy_count_mismatch"
        plan_payload["copy_tone"] = "rational"
    else:
        reason = "diversity_tuning"
        plan_payload["rerank_focus"] = "diversity"

    context["reflection_hint"] = reason
    context["reflection_count"] = reflection_count

    trace, latency = _node_meta("reflection", start)
    trace[0]["reason"] = reason
    return {
        "reflection_count": reflection_count,
        "context": context,
        "plan_payload": plan_payload,
        "reflection_notes": [{"iteration": reflection_count, "reason": reason}],
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


async def aggregate_node(state: PipelineState) -> PipelineState:
    start = time.perf_counter()
    trace, latency = _node_meta("aggregate", start)
    return {
        "total_latency_ms": (time.perf_counter() - state.get("_start_time", start)) * 1000,
        "trace_steps": trace,
        "node_latency_ms": latency,
    }


def route_after_marketing_copy(state: PipelineState) -> Literal["reflection", "aggregate"]:
    max_reflections = int(state.get("max_reflections", 1))
    reflection_count = int(state.get("reflection_count", 0))
    # Self-correction loop 必须有上限，质量检查不能把一次请求变成无限 graph run。
    if reflection_count >= max_reflections:
        return "aggregate"

    context = state.get("context", {})
    if isinstance(context, dict) and bool(context.get("force_reflection", False)):
        return "reflection"

    products = state.get("final_products", [])
    copies = state.get("marketing_copies", [])
    if not products:
        return "reflection"
    if len(copies) < len(products):
        return "reflection"
    # 当 category diversity 过低时触发 self-correction。
    categories: set[str] = set()
    for p in products:
        if isinstance(p, dict):
            cat = str(p.get("category", ""))
        else:
            cat = str(getattr(p, "category", ""))
        if cat:
            categories.add(cat)
    if len(products) >= 2 and len(categories) <= 1:
        return "reflection"
    return "aggregate"


@asynccontextmanager
async def recommendation_graph_context(checkpoint_path: str | None = None) -> AsyncIterator[Any]:
    settings = get_settings()
    backend = settings.checkpoint_backend.strip().lower()
    if backend != "sqlite":
        raise ValueError(f"Unsupported checkpoint backend: {settings.checkpoint_backend}")
    async with sqlite_checkpointer(checkpoint_path or settings.checkpoint_sqlite_path) as checkpointer:
        yield build_recommendation_graph(checkpointer=checkpointer)


def build_recommendation_graph(*, checkpointer: Any | None = None) -> Any:
    graph = StateGraph(PipelineState)
    # 先注册 workflow node；真正的执行顺序由下面的 edge 和 conditional edge 定义。
    graph.add_node("init", init_node)
    graph.add_node("planner", planner_node)
    graph.add_node("tool_router", tool_router_node)
    graph.add_node("tool_executor", tool_executor_node)
    graph.add_node("user_profile", user_profile_node)
    graph.add_node("product_recall", product_recall_node)
    graph.add_node("merge_phase1", merge_phase1_node)
    graph.add_node("rerank", rerank_node)
    graph.add_node("inventory", inventory_node)
    graph.add_node("merge_phase2", merge_phase2_node)
    graph.add_node("filter", filter_node)
    graph.add_node("inventory_repair", inventory_repair_node)
    graph.add_node("retention_offer", retention_offer_node)
    graph.add_node("marketing_copy", marketing_copy_node)
    graph.add_node("reflection", reflection_node)
    graph.add_node("aggregate", aggregate_node)

    graph.set_entry_point("init")
    graph.add_edge("init", "planner")
    graph.add_edge("planner", "tool_router")
    graph.add_edge("tool_router", "tool_executor")
    # User profiling 和 candidate recall 可以从同一个 planned state 并行展开。
    graph.add_edge("tool_executor", "user_profile")
    graph.add_edge("tool_executor", "product_recall")
    graph.add_edge("user_profile", "merge_phase1")
    graph.add_edge("product_recall", "merge_phase1")
    # Reranking 和 inventory check 使用同一批 recalled candidates，
    # 然后在 final filtering 前重新汇合。
    graph.add_edge("merge_phase1", "rerank")
    graph.add_edge("merge_phase1", "inventory")
    graph.add_edge("rerank", "merge_phase2")
    graph.add_edge("inventory", "merge_phase2")
    graph.add_edge("merge_phase2", "filter")
    graph.add_conditional_edges(
        "filter",
        route_after_filter,
        {
            "inventory_repair": "inventory_repair",
            "retention_offer": "retention_offer",
            "marketing_copy": "marketing_copy",
        },
    )
    graph.add_edge("inventory_repair", "marketing_copy")
    graph.add_edge("retention_offer", "marketing_copy")
    graph.add_conditional_edges(
        "marketing_copy",
        route_after_marketing_copy,
        {
            "reflection": "reflection",
            "aggregate": "aggregate",
        },
    )
    graph.add_edge("reflection", "tool_router")
    graph.add_edge("aggregate", END)
    return graph.compile(checkpointer=checkpointer)
