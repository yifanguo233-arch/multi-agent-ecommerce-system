# AutoPlanner 文件结构图
#
# 外部入口：
#   PlannerAgent / Graph
#          |
#          v
#   AutoPlanner.plan()
#          |
#          +-- _rule_plan()
#          |      规则生成完整 ExecutionPlan：
#          |      scene / trigger_event / risk_flags -> 策略默认值与 filters
#          |
#          +-- 是否启用 LLM?
#          |      否 -> rule_only，直接返回规则 plan
#          |      是 -> _llm_suggest()
#          |              |
#          |              +-- _parse_llm_json()
#          |              |      兼容 JSON、代码块、混杂文本和 key=value
#          |              |
#          |              +-- _merge_plan()
#          |                     白名单校验，只允许 refine 少数字段
#          |
#          +-- LLM 超时 / 解析失败 / 异常 -> rule_fallback
#          |
#          v
#   返回 ExecutionPlan，供 ProductRec / MarketingCopy / Inventory / Tool Router 消费
#
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config import get_settings
from models.schemas import ExecutionPlan

PLANNER_REPAIR_MAX_ATTEMPTS = 2


class AutoPlanner:
    """基于规则生成 execution plan，并可选使用 LLM 做 refine。"""

    # LLM refine 阶段只允许修改这些字段，其他内容仍由确定性代码控制。
    _PLAN_KEYS = {"retrieve_strategy", "rerank_focus", "copy_tone", "risk_policy"}

    _PLAN_KEYS = {"retrieve_strategy", "rerank_focus", "copy_tone", "risk_policy"}
    _ALLOWED_PLAN_VALUES = {
        "retrieve_strategy": {"semantic_first", "hot_first", "inventory_first", "hybrid"},
        "rerank_focus": {"price_first", "brand_first", "diversity", "balanced", "intent_match"},
        "copy_tone": {"rational", "promotion", "new_release", "reassure", "default"},
        "risk_policy": {"retention_boost", "stock_guard", "standard"},
    }

    def __init__(self):
        settings = get_settings()
        self.settings = settings
        self.use_llm = settings.planner_use_llm and bool(settings.llm_api_key)
        self.llm_timeout = settings.planner_llm_timeout_seconds
        self.llm: ChatOpenAI | None = None
        if self.use_llm:
            self.llm = ChatOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                temperature=0.1,
                max_tokens=settings.planner_llm_max_tokens,
            )

    async def plan(
        self,
        scene: str,
        user_context: dict[str, Any] | None = None,
        request_context: dict[str, Any] | None = None,
        business_goal: str = "conversion",
    ) -> ExecutionPlan:
        # 先由规则生成完整合法的 plan，LLM 结果只作为可选 refine，不作为唯一来源。
        base = self._rule_plan(scene, user_context, request_context, business_goal)
        if not self.use_llm:
            base.metadata["planner_mode"] = "rule_only"
            return base

        try:
            llm_plan, llm_meta = await asyncio.wait_for(
                self._llm_suggest(scene, user_context or {}, request_context or {}, business_goal),
                timeout=self.llm_timeout,
            )
            merged = self._merge_plan(base, llm_plan)
            merged.metadata["planner_mode"] = "rule_llm"
            merged.metadata.update(llm_meta)
            return merged
        except asyncio.TimeoutError:
            base.metadata["planner_mode"] = "rule_fallback"
            base.metadata["planner_error"] = f"llm_timeout_after_{self.llm_timeout}s"
            return base
        except Exception as exc:
            base.metadata["planner_mode"] = "rule_fallback"
            base.metadata["planner_error"] = str(exc)
            return base

    def _rule_plan(
        self,
        scene: str,
        user_context: dict[str, Any] | None,
        request_context: dict[str, Any] | None,
        business_goal: str,
    ) -> ExecutionPlan:
        uc = user_context or {}
        rc = request_context or {}
        risk_flags = uc.get("risk_flags", []) if isinstance(uc, dict) else []
        preference = uc.get("preference", {}) if isinstance(uc, dict) else {}
        price_sensitivity = float(preference.get("price_sensitivity", 0.5))
        trigger = str(rc.get("trigger_event", "browse"))
        retrieve_strategy = "hybrid"
        rerank_focus = "balanced"
        copy_tone = "default"
        risk_policy = "standard"
        filters: dict[str, Any] = {}

        # 详情页流量通常有更强的当前商品意图，因此优先使用语义召回和意图匹配重排。
        if scene in ("detail", "pdp") or trigger in ("detail_view", "add_to_cart"):
            retrieve_strategy = "semantic_first"
            rerank_focus = "intent_match"

        # 价格敏感用户优先看到低价或促销候选，文案也更强调性价比。
        if "high_price_sensitivity" in risk_flags or price_sensitivity >= 0.8:
            rerank_focus = "price_first"
            copy_tone = "promotion"
            filters["max_discount_priority"] = True

        # 高流失风险用户在排序和文案生成阶段都使用更温和的留存策略。
        if "high_churn_risk" in risk_flags:
            risk_policy = "retention_boost"
            copy_tone = "reassure"

        # 首页推荐即使存在强近期意图，也要避免结果过窄。
        if scene == "homepage":
            filters["diversity_boost"] = True

        return ExecutionPlan(
            plan_version="v1",
            retrieve_strategy=retrieve_strategy,
            rerank_focus=rerank_focus,
            copy_tone=copy_tone,
            risk_policy=risk_policy,
            business_goal=business_goal or "conversion",
            filters=filters,
            metadata={"scene": scene, "trigger_event": trigger},
        )

    async def _llm_suggest(
        self,
        scene: str,
        user_context: dict[str, Any],
        request_context: dict[str, Any],
        business_goal: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.llm is None:
            raise RuntimeError("LLM planner is disabled")

        schema = {
            "retrieve_strategy": ["semantic_first", "hot_first", "inventory_first", "hybrid"],
            "rerank_focus": ["price_first", "brand_first", "diversity", "balanced", "intent_match"],
            "copy_tone": ["rational", "promotion", "new_release", "reassure", "default"],
            "risk_policy": ["retention_boost", "stock_guard", "standard"],
        }
        prompt = {
            "scene": scene,
            "business_goal": business_goal,
            "user_context": user_context,
            "request_context": request_context,
            "output_schema": schema,
            # Planner 结果会被代码消费，因此模型必须返回可解析、可确定性校验的字段。
            "instruction": "Return one JSON object only. Do not include markdown, comments, analysis, or extra text. Keep stable and conservative.",
        }
        messages = [
            SystemMessage(content="You are an e-commerce planning agent. Output only valid JSON."),
            HumanMessage(content=json.dumps(prompt, ensure_ascii=False)),
        ]
        raw_outputs: list[tuple[str, str]] = []
        retry_used = False
        retry_attempts = 0
        plan: dict[str, Any] = {}
        try:
            response = await self.llm.ainvoke(messages)
            raw = str(response.content).strip()
            raw_outputs.append(("initial", raw))
            plan = self._valid_plan_delta(self._parse_llm_json(raw))
        except Exception as exc:
            raw_outputs.append(("initial_error", str(exc)))

        if not plan:
            retry_used = True
            for attempt in range(1, PLANNER_REPAIR_MAX_ATTEMPTS + 1):
                retry_attempts = attempt
                repair_messages = self._build_plan_repair_messages(
                    attempt=attempt,
                    prompt=prompt,
                    previous_output=raw_outputs[-1][1],
                )
                try:
                    repair_response = await self.llm.ainvoke(repair_messages)
                    repair_raw = str(repair_response.content).strip()
                except Exception as exc:
                    repair_raw = f"request_error: {exc}"
                raw_outputs.append((f"retry_{attempt}", repair_raw))
                try:
                    plan = self._valid_plan_delta(self._parse_llm_json(repair_raw))
                except Exception:
                    plan = {}
                if plan:
                    break

        if not plan:
            raise ValueError("LLM response did not contain valid plan fields")

        return plan, {
            "llm_parse_ok": True,
            "retry_used": retry_used,
            "retry_attempts": retry_attempts,
            "retry_succeeded": retry_used and retry_attempts > 0,
            "raw_response": self._format_raw_plan_outputs(raw_outputs),
        }

    def _build_plan_repair_messages(
        self,
        attempt: int,
        prompt: dict[str, Any],
        previous_output: str,
    ) -> list[Any]:
        return [
            SystemMessage(
                content=(
                    "You are a strict JSON formatter for an e-commerce execution plan.\n"
                    "Do not explain. Do not use markdown. Return one JSON object only."
                )
            ),
            HumanMessage(
                content=(
                    f"Repair attempt: {attempt}/{PLANNER_REPAIR_MAX_ATTEMPTS}\n"
                    f"Planning input: {json.dumps(prompt, ensure_ascii=False, default=str)}\n"
                    f"Previous invalid output: {previous_output[:1200]}\n"
                    "Return only these optional keys, using allowed values from output_schema:\n"
                    "{\n"
                    '  "retrieve_strategy": "semantic_first|hot_first|inventory_first|hybrid",\n'
                    '  "rerank_focus": "price_first|brand_first|diversity|balanced|intent_match",\n'
                    '  "copy_tone": "rational|promotion|new_release|reassure|default",\n'
                    '  "risk_policy": "retention_boost|stock_guard|standard"\n'
                    "}"
                )
            ),
        ]

    def _valid_plan_delta(self, candidate: dict[str, Any]) -> dict[str, Any]:
        valid: dict[str, Any] = {}
        for key, allowed_values in self._ALLOWED_PLAN_VALUES.items():
            value = candidate.get(key)
            if isinstance(value, str) and value in allowed_values:
                valid[key] = value
        return valid

    @staticmethod
    def _format_raw_plan_outputs(outputs: list[tuple[str, str]]) -> str:
        return "\n\n".join(f"---{label}---\n{raw}" for label, raw in outputs)

    def _parse_llm_json(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        candidates: list[dict[str, Any]] = []

        # 有些模型即使被要求不要输出 markdown，也仍会把 JSON 包在代码块里。
        for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
            parsed = self._try_parse_json_object(block.strip())
            if parsed is not None:
                candidates.append(parsed)

        # 兼容 OpenAI 风格推理模型可能输出的显式/隐式思考块，先移除再解析 JSON。
        text_without_thought = re.sub(
            r"<think>.*?</think>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        parsed = self._try_parse_json_object(text_without_thought)
        if parsed is not None:
            candidates.append(parsed)

        # 如果响应里混有解释文字和 JSON 对象，raw_decode 可以更稳地恢复 JSON。
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text_without_thought):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(text_without_thought[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                candidates.append(parsed)

        for candidate in candidates:
            if self._PLAN_KEYS.intersection(candidate):
                return candidate
        if candidates:
            return candidates[-1]

        # 最后尝试从 key-value 文本里抽取计划字段，避免模型没返回合法 JSON 时完全失败。
        extracted = self._extract_plan_fields(text_without_thought) or self._extract_plan_fields(text)
        if extracted:
            return extracted
        raise ValueError("LLM response did not contain JSON or plan fields")

    @staticmethod
    def _try_parse_json_object(raw: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _extract_plan_fields(self, raw: str) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        key_pattern = "|".join(sorted(self._PLAN_KEYS))
        for match in re.finditer(
            rf"\b({key_pattern})\b\s*[:=]\s*[\"'`]?([a-z_]+)[\"'`]?",
            raw,
            flags=re.IGNORECASE,
        ):
            fields[match.group(1)] = match.group(2)
        return fields

    def _merge_plan(self, base: ExecutionPlan, llm_plan: dict[str, Any]) -> ExecutionPlan:
        # 用小白名单校验模型建议；未知值回退到 rule plan，避免污染下游 Agent。
        allowed_retrieve = {"semantic_first", "hot_first", "inventory_first", "hybrid"}
        allowed_rerank = {"price_first", "brand_first", "diversity", "balanced", "intent_match"}
        allowed_tone = {"rational", "promotion", "new_release", "reassure", "default"}
        allowed_risk = {"retention_boost", "stock_guard", "standard"}
        retrieve = str(llm_plan.get("retrieve_strategy", base.retrieve_strategy))
        rerank = str(llm_plan.get("rerank_focus", base.rerank_focus))
        tone = str(llm_plan.get("copy_tone", base.copy_tone))
        risk = str(llm_plan.get("risk_policy", base.risk_policy))
        return ExecutionPlan(
            plan_version=base.plan_version,
            retrieve_strategy=retrieve if retrieve in allowed_retrieve else base.retrieve_strategy,
            rerank_focus=rerank if rerank in allowed_rerank else base.rerank_focus,
            copy_tone=tone if tone in allowed_tone else base.copy_tone,
            risk_policy=risk if risk in allowed_risk else base.risk_policy,
            business_goal=base.business_goal,
            filters=base.filters,
            metadata=base.metadata,
        )
