import asyncio

from agents.user_profile_agent import UserProfileAgent
from services.feature_store import FeatureStore
from services.memory_context import MemoryContextEngine


class FakeRedis:
    def __init__(self):
        self.sorted_sets = {}
        self.values = {}

    async def zadd(self, key, mapping):
        bucket = self.sorted_sets.setdefault(key, [])
        for member, score in mapping.items():
            bucket.append((float(score), member))

    async def expire(self, key, ttl):
        return True

    async def zrangebyscore(self, key, min_score, max_score):
        max_value = float("inf") if max_score == "+inf" else float(max_score)
        rows = [
            member
            for score, member in sorted(self.sorted_sets.get(key, []))
            if float(min_score) <= score <= max_value
        ]
        return [item.encode("utf-8") for item in rows]

    async def get(self, key):
        value = self.values.get(key)
        return value.encode("utf-8") if isinstance(value, str) else value

    async def set(self, key, value, ex=None):
        self.values[key] = value
        return True


def test_feature_store_records_and_builds_redis_features():
    redis = FakeRedis()
    store = FeatureStore(redis_client=redis)

    async def scenario():
        await store.record_behavior("u1", "view", "P001", {"category": "手机"})
        await store.record_behavior("u1", "click", "P003", {"category": "耳机"})
        await store.record_behavior("u1", "purchase", "P007", {"category": "配件", "amount": 399})
        await store.merge_offline_tags(
            "u1",
            {
                "preferred_categories_30d": ["手机"],
                "preferred_brands_30d": ["Apple"],
                "price_sensitivity": 0.8,
            },
        )
        return await store.get_user_features("u1")

    features = asyncio.run(scenario())

    assert features["source"] == "redis"
    assert features["recent_views"] == ["手机"]
    assert features["recent_clicks"] == ["耳机"]
    assert features["recent_purchases"] == ["配件"]
    assert features["offline_tags"]["preferred_brands_30d"] == ["Apple"]
    assert features["rfm"]["frequency"] > 0


def test_memory_context_uses_redis_feature_store_features():
    class StubFeatureStore:
        async def get_user_features(self, user_id: str):
            return {
                "recent_views": ["手机", "手机", "耳机"],
                "recent_clicks": ["手机"],
                "recent_purchases": [],
                "view_count_1h": 3,
                "offline_tags": {
                    "preferred_categories_30d": ["手机"],
                    "preferred_brands_30d": ["Apple"],
                    "price_sensitivity": 0.9,
                },
            }

    engine = MemoryContextEngine(feature_store=StubFeatureStore())
    snapshot = asyncio.run(engine.build("u1", "homepage", {}))

    assert snapshot.short_term.intent_categories[0] == "手机"
    assert snapshot.short_term.active_minutes_30m == 6
    assert snapshot.long_term.preferred_categories_30d == ["手机"]
    assert "high_price_sensitivity" in snapshot.risk_flags


def test_user_profile_agent_reads_feature_store_directly():
    class StubFeatureStore:
        async def get_user_features(self, user_id: str):
            return {
                "recent_views": ["耳机", "耳机", "手机"],
                "recent_clicks": ["耳机"],
                "recent_purchases": ["配件"],
                "view_count_24h": 3,
                "purchase_count_7d": 1,
                "rfm": {"recency": 1.0, "frequency": 0.1, "monetary": 0.4},
                "offline_tags": {
                    "preferred_categories_30d": ["耳机"],
                    "preferred_brands_30d": ["Sony"],
                    "price_sensitivity": 0.7,
                },
                "source": "redis",
            }

    agent = UserProfileAgent(feature_store=StubFeatureStore())
    behavior = asyncio.run(
        agent._collect_behavior(
            "u1",
            {
                "recent_views": ["平板"],
                "user_context": {
                    "short_term": {"recent_views_1h": ["手机"]},
                    "long_term": {"preferred_categories_30d": ["手机"]},
                    "preference": {"price_sensitivity": 0.2},
                },
            },
        )
    )

    assert behavior["feature_source"] == "redis"
    assert behavior["recent_views"] == ["耳机", "耳机", "手机"]
    assert behavior["recent_clicks"] == ["耳机"]
    assert behavior["purchase_count_30d"] == 1
    assert behavior["preferred_categories_30d"] == ["耳机"]
    assert behavior["price_sensitivity"] == 0.7
    assert behavior["rfm"]["recency"] == 1.0
