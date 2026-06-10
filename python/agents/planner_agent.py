"""
PlannerAgent 结构图

输入：
  scene / user_context / request_context / business_goal
      |
      v
PlannerAgent.run()
      |
      v
AutoPlanner.plan()
  - 规则先生成完整 ExecutionPlan
  - 可选 LLM refine 少数字段
      |
      +-- 成功：PlannerResult.execution_plan
      +-- 超时/异常：默认安全 plan

输出去向：
  execution_plan / plan_payload
      -> ProductRecAgent / InventoryAgent / MarketingCopyAgent / Tool Router

LLM 与兜底：
  AutoPlanner 可选调用 LLM；PlannerAgent 自身再包一层 timeout。
  失败时返回 hybrid + balanced + default + standard 的保守 plan。

核心分支：
  scene、trigger_event、risk_flags、price_sensitivity 影响策略字段。

当前边界：
  business_goal 当前主要透传；stock_guard 在 Planner 白名单内，但业务消费较弱。
"""

from __future__ import annotations

import asyncio
from typing import Any

from config import get_settings
from models.schemas import ExecutionPlan, PlannerResult
from services.auto_planner import AutoPlanner

from .base_agent import BaseAgent


class PlannerAgent(BaseAgent):
    """生成推荐执行计划，并在超时或异常时安全降级。"""

    def __init__(self, planner: AutoPlanner | None = None):
        settings = get_settings()
        super().__init__(name="planner", timeout=settings.agent_timeout_planner, max_retries=1)
        # AutoPlanner 负责具体规划策略；这个 Agent 包装层负责和其他 Agent 保持统一运行契约。
        self.planner = planner or AutoPlanner()

    async def _execute(self, **kwargs: Any) -> PlannerResult:
        scene = str(kwargs.get("scene", "homepage"))
        user_context = kwargs.get("user_context", {}) or {}
        request_context = kwargs.get("request_context", {}) or {}
        business_goal = str(kwargs.get("business_goal", "conversion"))
        # Planner 阶段不能拖垮整条推荐链路；如果规则加 LLM 规划太慢，基类会返回安全默认 plan。
        plan = await asyncio.wait_for(
            self.planner.plan(
                scene=scene,
                user_context=user_context,
                request_context=request_context,
                business_goal=business_goal,
            ),
            timeout=self.timeout,
        )
        planner_mode = str(plan.metadata.get("planner_mode", "")) if isinstance(plan.metadata, dict) else ""
        return PlannerResult(
            success=True,
            plan_hit=True,
            execution_plan=plan,
            # 把规划来源写入 Agent 结果，方便 graph replay 解释计划来自规则、LLM 细化还是降级。
            data={
                "llm_called": planner_mode == "rule_llm",
                "fallback": planner_mode in {"rule_fallback", "agent_fallback"},
                "fallback_reason": str(plan.metadata.get("planner_error", "")) if isinstance(plan.metadata, dict) else "",
                "planner_mode": planner_mode,
                "llm_parse_ok": bool(plan.metadata.get("llm_parse_ok", False)) if isinstance(plan.metadata, dict) else False,
                "retry_used": bool(plan.metadata.get("retry_used", False)) if isinstance(plan.metadata, dict) else False,
                "retry_attempts": int(plan.metadata.get("retry_attempts", 0)) if isinstance(plan.metadata, dict) else 0,
                "retry_succeeded": bool(plan.metadata.get("retry_succeeded", False)) if isinstance(plan.metadata, dict) else False,
            },
            confidence=0.95,
        )

    def _fallback(self, latency_ms: float, exc: Exception) -> PlannerResult:
        # 即使 Planner 自身失败，也用保守默认值保证下游节点还能继续执行。
        return PlannerResult(
            success=False,
            latency_ms=latency_ms,
            error=str(exc),
            confidence=0.0,
            plan_hit=False,
            data={
                "llm_called": False,
                "fallback": True,
                "fallback_reason": str(exc),
                "planner_mode": "agent_fallback",
                "llm_parse_ok": False,
                "retry_used": False,
                "retry_attempts": 0,
                "retry_succeeded": False,
            },
            execution_plan=ExecutionPlan(
                plan_version="v1",
                retrieve_strategy="hybrid",
                rerank_focus="balanced",
                copy_tone="default",
                risk_policy="standard",
                business_goal="conversion",
                metadata={"planner_mode": "agent_fallback", "planner_error": str(exc)},
            ),
        )
