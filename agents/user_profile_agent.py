from __future__ import annotations

# UserProfileAgent 结构图
#
# 输入：
#   user_id + context
#       |
#       +-- request context：请求里直接传入的浏览、购买、偏好等字段
#       +-- user_context：Graph/MemoryContext 生成的短期、长期、意图和风险信息
#       +-- Redis FeatureStore：实时特征和离线标签
#       |
#       v
# _collect_behavior()
#       |
#       v
# LLM 画像分析
#       |
#       +-- 成功：解析为 UserProfile
#       +-- 失败：_rule_based_profile() 规则画像兜底
#
# 输出去向：
#   UserProfileResult.profile
#       -> ProductRecAgent 做个性化召回/重排
#       -> MarketingCopyAgent 选择分群文案模板
#
# LLM 与兜底：
#   LLM 负责把行为数据转成 segments、preferred_categories、price_range、RFM。
#   LLM 调用或解析失败时，根据购买次数、浏览次数、客单价生成规则画像。
#
# 核心分支：
#   purchase_count_30d、avg_order_amount、view_count_7d 决定用户分群。
#   Redis / user_context / request context 按可用性合并，Redis 可缺席。
#
# 当前边界：
#   画像是轻量规则 + LLM 结果，不是完整用户画像系统；Redis 不可用时仍可运行。
#
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config import get_settings
from models.schemas import UserProfile, UserProfileResult, UserSegment
from services.feature_store import FeatureStore

from .base_agent import BaseAgent

SYSTEM_PROMPT = """You are an e-commerce user profiling expert. Analyze behavior data and output JSON only.

Expected JSON format:
{
  "segments": ["new_user"|"active"|"high_value"|"price_sensitive"|"churn_risk"],
  "preferred_categories": ["category1", "category2"],
  "price_range": [min_price, max_price],
  "rfm_score": {"recency": 0-1, "frequency": 0-1, "monetary": 0-1},
  "real_time_tags": {"active_period": "...", "style_preference": "..."}
}
"""

PROFILE_REPAIR_MAX_ATTEMPTS = 2


class UserProfileAgent(BaseAgent):
    def __init__(self, feature_store: FeatureStore | None = None):
        settings = get_settings()
        super().__init__(
            name="user_profile",
            timeout=settings.agent_timeout_user_profile,
        )
        self.llm: ChatOpenAI | None = None
        if settings.llm_api_key:
            self.llm = ChatOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                temperature=0.3,
                max_tokens=1024,
            )
        self.feature_store = feature_store

    async def _execute(self, **kwargs: Any) -> UserProfileResult:
        user_id: str = kwargs["user_id"]
        context: dict = kwargs.get("context", {})

        behavior_data = await self._collect_behavior(user_id, context)

        if self.llm is None:
            profile_data = self._rule_based_profile(user_id, behavior_data)
            return UserProfileResult(
                success=True,
                profile=profile_data,
                data={
                    "raw_analysis": "",
                    "fallback": True,
                    "fallback_reason": "llm_api_key_not_configured",
                    "llm_called": False,
                    "llm_parse_ok": False,
                    "retry_used": False,
                    "retry_attempts": 0,
                    "retry_succeeded": False,
                },
                confidence=0.6,
            )

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"user_id: {user_id}\nbehavior_data: {json.dumps(behavior_data, ensure_ascii=False)}"),
        ]

        raw_outputs: list[tuple[str, str]] = []
        retry_used = False
        retry_attempts = 0
        try:
            try:
                response = await self.llm.ainvoke(messages)
                raw_response = str(response.content)
                raw_outputs.append(("initial", raw_response))
                profile_data, parse_ok = self._parse_profile_strict(user_id, raw_response)
            except Exception as exc:
                profile_data, parse_ok = None, False
                raw_outputs.append(("initial_error", str(exc)))
            if not parse_ok:
                retry_used = True
                for attempt in range(1, PROFILE_REPAIR_MAX_ATTEMPTS + 1):
                    retry_attempts = attempt
                    repair_messages = self._build_profile_repair_messages(
                        attempt=attempt,
                        user_id=user_id,
                        behavior_data=behavior_data,
                        previous_output=raw_outputs[-1][1],
                    )
                    try:
                        repair_response = await self.llm.ainvoke(repair_messages)
                        repair_raw = str(repair_response.content)
                    except Exception as exc:
                        repair_raw = f"request_error: {exc}"
                    raw_outputs.append((f"retry_{attempt}", repair_raw))
                    profile_data, parse_ok = self._parse_profile_strict(user_id, repair_raw)
                    if parse_ok:
                        break
            if not parse_ok or profile_data is None:
                raise ValueError("profile output parse empty")
            return UserProfileResult(
                success=True,
                profile=profile_data,
                data={
                    "raw_analysis": self._format_raw_profile_outputs(raw_outputs),
                    "fallback": False,
                    "fallback_reason": "",
                    "llm_called": True,
                    "llm_parse_ok": True,
                    "retry_used": retry_used,
                    "retry_attempts": retry_attempts,
                    "retry_succeeded": retry_used and retry_attempts > 0,
                },
                confidence=0.85,
            )
        except Exception as exc:
            profile_data = self._rule_based_profile(user_id, behavior_data)
            return UserProfileResult(
                success=True,
                profile=profile_data,
                data={
                    "raw_analysis": self._format_raw_profile_outputs(raw_outputs),
                    "fallback": True,
                    "fallback_reason": str(exc),
                    "llm_called": True,
                    "llm_parse_ok": False,
                    "retry_used": retry_used,
                    "retry_attempts": retry_attempts,
                    "retry_succeeded": False,
                },
                confidence=0.6,
                error=str(exc),
            )

    async def _collect_behavior(self, user_id: str, context: dict) -> dict:
        """从 Redis FeatureStore 和请求上下文中汇总用户行为。"""
        redis_data: dict[str, Any] = {}
        redis_error = ""
        if self.feature_store:
            try:
                redis_data = await self.feature_store.get_user_features(user_id)
            except Exception as exc:
                redis_error = str(exc)

        user_context_data = self._behavior_from_user_context(user_id, context)
        request_data = self._behavior_from_request_context(user_id, context)
        behavior = self._merge_behavior_sources(
            user_id=user_id,
            request_data=request_data,
            user_context_data=user_context_data,
            redis_data=redis_data,
        )
        if redis_error:
            behavior["feature_store_error"] = redis_error
        return behavior

    def _behavior_from_user_context(self, user_id: str, context: dict) -> dict[str, Any]:
        user_context = context.get("user_context")
        if isinstance(user_context, dict):
            short_term = user_context.get("short_term", {})
            long_term = user_context.get("long_term", {})
            preference = user_context.get("preference", {})
            return {
                "user_id": user_id,
                "recent_views": short_term.get("recent_views_1h", []),
                "recent_clicks": short_term.get("recent_clicks_1h", []),
                "recent_purchases": short_term.get("recent_purchases_24h", []),
                "view_count_7d": context.get("view_count_7d", len(short_term.get("recent_views_1h", []))),
                "purchase_count_30d": context.get("purchase_count_30d", 0),
                "avg_order_amount": long_term.get("avg_order_amount_30d", 299.0),
                "active_hours": context.get("active_hours", [20, 21, 22]),
                "preferred_categories_30d": long_term.get("preferred_categories_30d", []),
                "preferred_brands_30d": long_term.get("preferred_brands_30d", []),
                "price_sensitivity": preference.get("price_sensitivity", 0.5),
                "feature_source": "user_context",
            }
        return {}

    def _behavior_from_request_context(self, user_id: str, context: dict) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "recent_views": context.get("recent_views", ["手机", "耳机", "平板"]),
            "recent_clicks": context.get("recent_clicks", []),
            "recent_purchases": context.get("recent_purchases", ["充电器"]),
            "view_count_7d": context.get("view_count_7d", 25),
            "purchase_count_30d": context.get("purchase_count_30d", 3),
            "avg_order_amount": context.get("avg_order_amount", 299.0),
            "active_hours": context.get("active_hours", [20, 21, 22]),
            "preferred_categories_30d": context.get("preferred_categories_30d", []),
            "preferred_brands_30d": context.get("preferred_brands_30d", []),
            "price_sensitivity": context.get("price_sensitivity", 0.5),
            "feature_source": "request_context",
        }

    def _merge_behavior_sources(
        self,
        user_id: str,
        request_data: dict[str, Any],
        user_context_data: dict[str, Any],
        redis_data: dict[str, Any],
    ) -> dict[str, Any]:
        offline_tags = redis_data.get("offline_tags", {}) if isinstance(redis_data, dict) else {}
        redis_enriched = {
            **redis_data,
            "view_count_7d": redis_data.get("view_count_7d", redis_data.get("view_count_24h")),
            "purchase_count_30d": redis_data.get(
                "purchase_count_30d",
                redis_data.get("purchase_count_7d"),
            ),
            "preferred_categories_30d": offline_tags.get("preferred_categories_30d", []),
            "preferred_brands_30d": offline_tags.get("preferred_brands_30d", []),
            "price_sensitivity": offline_tags.get("price_sensitivity", redis_data.get("price_sensitivity")),
        }
        source_priority = [request_data, user_context_data, redis_enriched]

        def first_non_empty(key: str, default: Any) -> Any:
            for source in reversed(source_priority):
                value = source.get(key) if isinstance(source, dict) else None
                if value not in (None, [], {}, ""):
                    return value
            return default

        return {
            "user_id": user_id,
            "recent_views": first_non_empty("recent_views", []),
            "recent_clicks": first_non_empty("recent_clicks", []),
            "recent_purchases": first_non_empty("recent_purchases", []),
            "view_count_7d": int(
                first_non_empty(
                    "view_count_7d",
                    first_non_empty("view_count_24h", request_data.get("view_count_7d", 0)),
                )
            ),
            "purchase_count_30d": int(
                first_non_empty(
                    "purchase_count_30d",
                    first_non_empty("purchase_count_7d", request_data.get("purchase_count_30d", 0)),
                )
            ),
            "avg_order_amount": float(first_non_empty("avg_order_amount", 299.0)),
            "active_hours": first_non_empty("active_hours", [20, 21, 22]),
            "preferred_categories_30d": first_non_empty(
                "preferred_categories_30d",
                offline_tags.get("preferred_categories_30d", []),
            ),
            "preferred_brands_30d": first_non_empty(
                "preferred_brands_30d",
                offline_tags.get("preferred_brands_30d", []),
            ),
            "price_sensitivity": float(
                first_non_empty("price_sensitivity", offline_tags.get("price_sensitivity", 0.5))
            ),
            "rfm": first_non_empty("rfm", {}),
            "feature_source": (
                "redis"
                if redis_data
                else user_context_data.get("feature_source", request_data.get("feature_source", "request_context"))
            ),
        }

    def _build_profile_repair_messages(
        self,
        attempt: int,
        user_id: str,
        behavior_data: dict[str, Any],
        previous_output: str,
    ) -> list[Any]:
        return [
            SystemMessage(
                content=(
                    "You are a strict JSON formatter for an e-commerce user profile.\n"
                    "Do not explain. Do not use markdown. Return one JSON object only."
                )
            ),
            HumanMessage(
                content=(
                    f"Repair attempt: {attempt}/{PROFILE_REPAIR_MAX_ATTEMPTS}\n"
                    f"user_id: {user_id}\n"
                    f"behavior_data: {json.dumps(behavior_data, ensure_ascii=False, default=str)}\n"
                    f"previous invalid output: {previous_output[:1200]}\n"
                    "Return JSON with exactly these keys:\n"
                    "{\n"
                    '  "segments": ["new_user"|"active"|"high_value"|"price_sensitive"|"churn_risk"],\n'
                    '  "preferred_categories": ["category"],\n'
                    '  "price_range": [0, 10000],\n'
                    '  "rfm_score": {"recency": 0.0, "frequency": 0.0, "monetary": 0.0},\n'
                    '  "real_time_tags": {"key": "value"}\n'
                    "}"
                )
            ),
        ]

    def _parse_profile_strict(self, user_id: str, raw: str) -> tuple[UserProfile | None, bool]:
        data = self._extract_profile_json(raw)
        if not isinstance(data, dict):
            return None, False
        required = {"segments", "preferred_categories", "price_range", "rfm_score"}
        if not required.issubset(data):
            return None, False
        try:
            profile = self._profile_from_data(user_id, data)
        except Exception:
            return None, False
        return profile, True

    def _parse_profile(self, user_id: str, raw: str) -> UserProfile:
        data = self._extract_profile_json(raw) or {}
        return self._profile_from_data(user_id, data)

    def _extract_profile_json(self, raw: str) -> dict[str, Any] | None:
        cleaned = raw.strip()
        if not cleaned:
            return None
        # 如果模型把 JSON 包在代码块里，优先提取代码块内容。
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        # 如果输出混有解释文字，则尝试提取第一个 JSON 对象。
        if not cleaned.startswith("{"):
            obj = re.search(r"\{[\s\S]*\}", cleaned)
            if obj:
                cleaned = obj.group(0)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _profile_from_data(self, user_id: str, data: dict[str, Any]) -> UserProfile:
        segments = []
        for s in data.get("segments", ["active"]):
            try:
                segments.append(UserSegment(s))
            except ValueError:
                continue

        price_range_raw = data.get("price_range", [0, 10000])
        price_range = (
            float(price_range_raw[0]),
            float(price_range_raw[1]) if len(price_range_raw) > 1 else 10000.0,
        )

        return UserProfile(
            user_id=user_id,
            segments=segments or [UserSegment.ACTIVE],
            preferred_categories=data.get("preferred_categories", []),
            price_range=price_range,
            rfm_score=data.get("rfm_score", {}),
            real_time_tags=data.get("real_time_tags", {}),
        )

    @staticmethod
    def _format_raw_profile_outputs(outputs: list[tuple[str, str]]) -> str:
        return "\n\n".join(f"---{label}---\n{raw}" for label, raw in outputs)

    def _rule_based_profile(self, user_id: str, behavior_data: dict[str, Any]) -> UserProfile:
        """LLM 不可用时，使用确定性规则生成用户画像。"""
        recent_views = behavior_data.get("recent_views", [])
        purchase_count_30d = int(behavior_data.get("purchase_count_30d", 0))
        view_count_7d = int(behavior_data.get("view_count_7d", 0))
        avg_order_amount = float(behavior_data.get("avg_order_amount", 299.0))

        segments: list[UserSegment] = [UserSegment.ACTIVE]
        if purchase_count_30d == 0:
            segments = [UserSegment.NEW_USER]
        elif avg_order_amount >= 2000:
            segments = [UserSegment.HIGH_VALUE]
        elif avg_order_amount <= 300:
            segments = [UserSegment.PRICE_SENSITIVE]
        elif view_count_7d <= 3:
            segments = [UserSegment.CHURN_RISK]

        preferred_categories = list(dict.fromkeys(str(x) for x in recent_views if x))
        if not preferred_categories:
            preferred_categories = ["手机", "耳机"]

        delta = max(200.0, avg_order_amount * 1.5)
        min_price = max(0.0, avg_order_amount - delta)
        max_price = avg_order_amount + delta

        return UserProfile(
            user_id=user_id,
            segments=segments,
            preferred_categories=preferred_categories,
            price_range=(round(min_price, 2), round(max_price, 2)),
            rfm_score={
                "recency": 0.6 if view_count_7d > 0 else 0.2,
                "frequency": min(1.0, view_count_7d / 30.0),
                "monetary": min(1.0, avg_order_amount / 1000.0),
            },
            real_time_tags={
                "active_hours": behavior_data.get("active_hours", [20, 21, 22]),
                "profile_source": "rule_based_fallback",
            },
        )
