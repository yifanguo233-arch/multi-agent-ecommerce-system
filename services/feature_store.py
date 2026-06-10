"""
实时特征存储服务
- Redis Sorted Set 存储用户行为序列 (score=timestamp)
- 滑动窗口计算实时特征 (1h/24h/7d)
- 离线+在线特征合并
- RFM模型计算
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

logger = structlog.get_logger()


class FeatureStore:
    """基于 Redis 的实时 FeatureStore，用于存储用户行为和画像特征。"""

    def __init__(self, redis_client: Any = None, ttl: int = 86400):
        self.redis = redis_client
        self.ttl = ttl

    # ---------- 行为追踪 ----------

    async def record_behavior(
        self, user_id: str, behavior_type: str, item_id: str, metadata: dict | None = None
    ):
        """把一条行为事件追加到用户的 sorted set 中，score 使用 timestamp。"""
        if not self.redis:
            return
        key = f"behavior:{user_id}:{behavior_type}"
        payload = json.dumps({"item_id": item_id, "ts": time.time(), **(metadata or {})})
        await self.redis.zadd(key, {payload: time.time()})
        await self.redis.expire(key, self.ttl)

    async def get_recent_behaviors(
        self, user_id: str, behavior_type: str, window_seconds: int = 3600
    ) -> list[dict]:
        """读取滑动时间窗口内的用户行为。"""
        if not self.redis:
            return []
        key = f"behavior:{user_id}:{behavior_type}"
        cutoff = time.time() - window_seconds
        raw_items = await self.redis.zrangebyscore(key, cutoff, "+inf")
        return [self._loads_json(item) for item in raw_items]

    # ---------- 实时 features ----------

    async def get_user_features(self, user_id: str) -> dict[str, Any]:
        """根据近期行为构建聚合后的 feature vector。"""
        views_1h = await self.get_recent_behaviors(user_id, "view", 3600)
        views_24h = await self.get_recent_behaviors(user_id, "view", 86400)
        clicks_1h = await self.get_recent_behaviors(user_id, "click", 3600)
        purchases_7d = await self.get_recent_behaviors(user_id, "purchase", 604800)

        recent_click_items = [self._event_interest(c) for c in clicks_1h[-20:]]
        recent_purchase_items = [self._event_interest(p) for p in purchases_7d[-10:]]
        recent_view_items = [self._event_interest(v) for v in views_24h[-20:]]

        rfm = await self._compute_rfm(user_id, purchases_7d)

        profile_key = f"profile:{user_id}"
        offline_tags = {}
        if self.redis:
            raw = await self.redis.get(profile_key)
            if raw:
                offline_tags = self._loads_json(raw)

        return {
            "user_id": user_id,
            "view_count_1h": len(views_1h),
            "view_count_24h": len(views_24h),
            "click_count_1h": len(clicks_1h),
            "purchase_count_7d": len(purchases_7d),
            "recent_views": recent_view_items,
            "recent_clicks": recent_click_items,
            "recent_purchases": recent_purchase_items,
            "rfm": rfm,
            "offline_tags": offline_tags,
            "source": "redis",
        }

    # ---------- RFM model ----------

    async def _compute_rfm(self, user_id: str, purchases: list[dict]) -> dict[str, float]:
        """
        计算 Recency / Frequency / Monetary 分数，归一化到 0-1。
        demo 场景没有完整数据，因此使用启发式估算。
        """
        if not purchases:
            return {"recency": 0.0, "frequency": 0.0, "monetary": 0.0}

        now = time.time()
        latest_ts = max(p.get("ts", 0) for p in purchases)
        days_since = (now - latest_ts) / 86400

        recency = max(0.0, 1.0 - days_since / 30.0)
        frequency = min(1.0, len(purchases) / 10.0)
        avg_amount = sum(p.get("amount", 100) for p in purchases) / len(purchases)
        monetary = min(1.0, avg_amount / 1000.0)

        return {
            "recency": round(recency, 3),
            "frequency": round(frequency, 3),
            "monetary": round(monetary, 3),
        }

    # ---------- 离线标签合并 ----------

    async def merge_offline_tags(self, user_id: str, tags: dict[str, Any]):
        """写入离线批处理标签，方便 profile Agent 后续读取。"""
        if not self.redis:
            return
        key = f"profile:{user_id}"
        await self.redis.set(key, json.dumps(tags), ex=self.ttl)

    @staticmethod
    def _loads_json(raw: Any) -> dict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    @staticmethod
    def _event_interest(event: dict[str, Any]) -> str:
        metadata_category = event.get("category")
        if metadata_category:
            return str(metadata_category)
        return str(event.get("item_id", ""))
