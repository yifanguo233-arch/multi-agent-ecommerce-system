"""
Supervisor编排器 — 并行分发 + 聚合模式

                    ┌──────────────┐
                    │  Supervisor   │
                    └──────┬───────┘
           ┌───────┬───────┼───────┬────────┐
           ▼       ▼       ▼       ▼        │
      UserProfile  ProdRec  MktCopy  Inventory │
           │       │       │       │        │
           └───────┴───────┴───────┘        │
                    │                        │
                    ▼                        │
               Aggregator ◄─────────────────┘
                    │
                    ▼
              A/B Test Engine
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Any

import structlog

from agents import (
    InventoryAgent,
    MarketingCopyAgent,
    PlannerAgent,
    ProductRecAgent,
    UserProfileAgent,
)
from models.schemas import (
    Product,
    RecommendationRequest,
    RecommendationResponse,
    UserProfile,
)
from services.ab_test import ABTestEngine
from services.feature_store import FeatureStore
from services.memory_context import MemoryContextEngine

logger = structlog.get_logger()


class SupervisorOrchestrator:
    """按并行分发再聚合的模式协调多个 Agent。"""

    def __init__(
        self,
        ab_engine: ABTestEngine | None = None,
        feature_store: FeatureStore | None = None,
    ):
        self.user_profile_agent = UserProfileAgent()
        self.user_profile_agent.feature_store = feature_store
        self.product_rec_agent = ProductRecAgent()
        self.marketing_copy_agent = MarketingCopyAgent()
        self.inventory_agent = InventoryAgent()
        self.ab_engine = ab_engine or ABTestEngine()
        self.memory_engine = MemoryContextEngine(feature_store)
        self.planner_agent = PlannerAgent()

    def configure_feature_store(self, feature_store: FeatureStore | None) -> None:
        self.user_profile_agent.feature_store = feature_store
        self.memory_engine = MemoryContextEngine(feature_store)

    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        request_id = str(uuid.uuid4())
        start = time.perf_counter()

        logger.info(
            "supervisor.start",
            request_id=request_id,
            user_id=request.user_id,
            scene=request.scene,
        )

        experiment = self.ab_engine.assign(request.user_id)
        experiment_group = str(experiment.get("group", "control"))
        experiment_config = (
            experiment.get("config", {})
            if isinstance(experiment.get("config", {}), dict)
            else {}
        )
        user_context = await self.memory_engine.build(
            user_id=request.user_id,
            scene=request.scene,
            context=request.context,
        )
        planner_result = await self.planner_agent.run(
            scene=request.scene,
            user_context=user_context.model_dump(mode="json"),
            request_context=request.context,
            business_goal=request.business_goal,
        )
        plan = planner_result.execution_plan
        user_context_payload = user_context.model_dump(mode="json")
        merged_context = {
            **request.context,
            "user_context": user_context_payload,
            "execution_plan": plan.model_dump(),
            "experiment_group": experiment_group,
            "ab_config": experiment_config,
        }

        # Phase 1：并行执行 user profile 和 product recall。
        profile_result, rec_result = await asyncio.gather(
            self.user_profile_agent.run(
                user_id=request.user_id,
                context=merged_context,
            ),
            self.product_rec_agent.run(
                user_profile=None,
                context=merged_context,
                num_items=request.num_items * 2,
            ),
        )

        user_profile: UserProfile | None = getattr(profile_result, "profile", None)
        raw_products: list[Product] = getattr(rec_result, "products", [])

        # Phase 2：使用 profile 做 re-rank，同时做 inventory check。
        rerank_kwargs: dict[str, Any] = {
            "user_profile": user_profile,
            "context": merged_context,
            "num_items": request.num_items,
        }
        run_signature = inspect.signature(self.product_rec_agent.run)
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in run_signature.parameters.values()
        )
        if accepts_kwargs or "candidates" in run_signature.parameters:
            rerank_kwargs["candidates"] = raw_products
        rerank_task = self.product_rec_agent.run(**rerank_kwargs)
        inventory_task = self.inventory_agent.run(products=raw_products, context=merged_context)

        rerank_result, inventory_result = await asyncio.gather(
            rerank_task, inventory_task
        )

        ranked_products: list[Product] = getattr(rerank_result, "products", raw_products)

        available_ids = set(getattr(inventory_result, "available_products", []))
        final_products = [p for p in ranked_products if p.product_id in available_ids]
        if not final_products:
            final_products = ranked_products[:request.num_items]
        final_products = final_products[:request.num_items]

        # Phase 3：基于最终商品列表生成 marketing copy。
        copy_result = await self.marketing_copy_agent.run(
            user_profile=user_profile,
            products=final_products,
            context=merged_context,
        )
        copies = getattr(copy_result, "copies", [])

        total_latency = (time.perf_counter() - start) * 1000
        planner_meta = plan.metadata if isinstance(plan.metadata, dict) else {}
        planner_mode = str(planner_meta.get("planner_mode", "unknown"))
        fallback_reason = str(planner_meta.get("planner_error", ""))
        latency_bucket = self._latency_bucket(total_latency)
        ab_group = experiment_group
        plan_replay_key = f"{request.user_id}:{request.scene}:{plan.plan_version}:{planner_mode}:{ab_group}"

        logger.info(
            "supervisor.complete",
            request_id=request_id,
            total_latency_ms=round(total_latency, 1),
            product_count=len(final_products),
            copy_count=len(copies),
        )

        return RecommendationResponse(
            request_id=request_id,
            user_id=request.user_id,
            products=final_products,
            marketing_copies=copies,
            experiment_group=experiment_group,
            context_version="v1",
            memory_hit=True,
            plan_version=plan.plan_version,
            plan_hit=planner_result.plan_hit,
            execution_plan=plan.model_dump(),
            plan_payload=plan.model_dump(),
            plan_replay_key=plan_replay_key,
            planner_observability={
                "planner_mode": planner_mode,
                "fallback_reason": fallback_reason,
                "latency_bucket": latency_bucket,
                "ab_group": ab_group,
            },
            debug_context={
                "user_context": user_context_payload,
                "execution_plan": plan.model_dump(),
                "planner_result": planner_result.model_dump(),
                "ab_config": experiment_config,
            },
            agent_results={
                "planner": planner_result,
                "user_profile": profile_result,
                "product_rec": rerank_result,
                "marketing_copy": copy_result,
                "inventory": inventory_result,
            },
            total_latency_ms=total_latency,
        )

    @staticmethod
    def _latency_bucket(latency_ms: float) -> str:
        if latency_ms < 200:
            return "lt_200ms"
        if latency_ms < 500:
            return "200_500ms"
        if latency_ms < 1000:
            return "500_1000ms"
        return "ge_1000ms"
