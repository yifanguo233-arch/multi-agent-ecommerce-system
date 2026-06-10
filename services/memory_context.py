from __future__ import annotations

from collections import Counter
from typing import Any

from models.schemas import LongTermMemory, ShortTermMemory, UserContextSnapshot
from services.feature_store import FeatureStore


class MemoryContextEngine:
    """根据 short-term 和 long-term 信号构建统一的 user context。"""

    def __init__(self, feature_store: FeatureStore | None = None):
        self.feature_store = feature_store

    async def build(
        self,
        user_id: str,
        scene: str,
        context: dict[str, Any] | None = None,
    ) -> UserContextSnapshot:
        raw_context = context or {}
        features = await self._get_features(user_id, raw_context)

        short_term = self._build_short_term(features, raw_context)
        long_term = self._build_long_term(features, raw_context)
        intent = self._build_intent(short_term, raw_context)
        preference = self._build_preference(long_term, raw_context)
        risk_flags = self._build_risk_flags(short_term, long_term)

        return UserContextSnapshot(
            user_id=user_id,
            scene=scene,
            short_term=short_term,
            long_term=long_term,
            intent=intent,
            preference=preference,
            risk_flags=risk_flags,
            freshness_seconds=0,
        )

    async def _get_features(self, user_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if self.feature_store:
            return await self.feature_store.get_user_features(user_id)
        return {
            "recent_views": context.get("recent_views", []),
            "recent_clicks": context.get("recent_clicks", []),
            "recent_purchases": context.get("recent_purchases", []),
            "view_count_1h": context.get("view_count_1h", 0),
            "purchase_count_7d": context.get("purchase_count_7d", 0),
            "offline_tags": context.get("offline_tags", {}),
        }

    def _build_short_term(self, features: dict[str, Any], context: dict[str, Any]) -> ShortTermMemory:
        views = [str(x) for x in features.get("recent_views", []) if x]
        clicks = [str(x) for x in features.get("recent_clicks", []) if x]
        purchases = [str(x) for x in features.get("recent_purchases", []) if x]

        return ShortTermMemory(
            session_id=str(context.get("session_id", "")),
            recent_views_1h=views[-20:],  #最后20条，即最新的
            recent_clicks_1h=clicks[-20:],
            recent_purchases_24h=purchases[-10:],
            intent_categories=self._top_values(views + clicks, top_k=3),  #意图分类（取浏览+点击中出现最多的3个）
            active_minutes_30m=int(features.get("view_count_1h", 0)) * 2,  #活跃时长计算
        )

    def _build_long_term(self, features: dict[str, Any], context: dict[str, Any]) -> LongTermMemory:
        offline = features.get("offline_tags", {}) or {}   #获取离线预计算的用户标签
        avg_order_amount = float(context.get("avg_order_amount", 0.0))  #从上下文中获取30天平均订单金额并转为浮点数

        return LongTermMemory(
            preferred_categories_30d=self._as_str_list(  #确保格式统一
                offline.get("preferred_categories_30d", context.get("preferred_categories_30d", []))  #前后顺序为优先级
            ),
            preferred_brands_30d=self._as_str_list(
                offline.get("preferred_brands_30d", context.get("preferred_brands_30d", []))  
            ),
            price_sensitivity=float(context.get("price_sensitivity", offline.get("price_sensitivity", 0.5))),   #价格敏感度，值越高表示用户对价格越敏感（倾向于买便宜货）
            avg_order_amount_30d=avg_order_amount,
            purchase_power_tier=self._purchase_power_tier(avg_order_amount),   #购买力
            churn_risk_score=float(offline.get("churn_risk_score", context.get("churn_risk_score", 0.0))),  #流失风险
        )

    def _build_intent(self, short_term: ShortTermMemory, context: dict[str, Any]) -> dict[str, Any]:
        trigger = str(context.get("trigger_event", "browse"))
        current_item = context.get("current_item", {})
        return {
            "trigger_event": trigger,
            "current_item": current_item,
            "focus_categories": short_term.intent_categories,
        }

    def _build_preference(self, long_term: LongTermMemory, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "preferred_categories": long_term.preferred_categories_30d,
            "preferred_brands": long_term.preferred_brands_30d,
            "price_sensitivity": long_term.price_sensitivity,
            "price_range_hint": context.get("price_range_hint", [0, 10000]),
        }

    def _build_risk_flags(
        self, short_term: ShortTermMemory, long_term: LongTermMemory
    ) -> list[str]:
        flags: list[str] = []
        if short_term.active_minutes_30m <= 2:
            flags.append("low_recent_activity")
        if long_term.churn_risk_score >= 0.7:
            flags.append("high_churn_risk")
        if long_term.price_sensitivity >= 0.8:
            flags.append("high_price_sensitivity")
        return flags

    def _purchase_power_tier(self, avg_order_amount: float) -> str:
        if avg_order_amount >= 3000:
            return "high"
        if avg_order_amount >= 800:
            return "mid"
        if avg_order_amount > 0:
            return "entry"
        return "unknown"

    def _top_values(self, values: list[str], top_k: int = 3) -> list[str]:
        if not values:
            return []
        counts = Counter(values)
        return [v for v, _ in counts.most_common(top_k)]

    def _as_str_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(x) for x in value if x]
        return []
