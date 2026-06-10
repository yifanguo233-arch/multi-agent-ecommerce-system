import asyncio

from services.memory_context import MemoryContextEngine


def test_build_context_with_request_fallback():
    engine = MemoryContextEngine(feature_store=None)
    context = {
        "session_id": "s-1",
        "recent_views": ["耳机", "耳机", "游戏机"],
        "recent_clicks": ["耳机"],
        "recent_purchases": ["P001"],
        "avg_order_amount": 3600.0,
        "price_sensitivity": 0.85,
        "offline_tags": {
            "preferred_categories_30d": ["平板", "手机"],
            "preferred_brands_30d": ["Apple"],
            "churn_risk_score": 0.8,
        },
        "trigger_event": "detail_view",
    }

    snapshot = asyncio.run(engine.build(user_id="u100", scene="detail", context=context))

    assert snapshot.user_id == "u100"
    assert snapshot.scene == "detail"
    assert snapshot.short_term.session_id == "s-1"
    assert snapshot.short_term.intent_categories[0] == "耳机"
    assert snapshot.long_term.purchase_power_tier == "high"
    assert "high_churn_risk" in snapshot.risk_flags
    assert "high_price_sensitivity" in snapshot.risk_flags
    assert snapshot.intent["trigger_event"] == "detail_view"


def test_build_context_uses_feature_store_when_available():
    class StubFeatureStore:
        async def get_user_features(self, user_id: str):
            return {
                "recent_views": ["手机", "手机", "平板"],
                "recent_clicks": ["手机"],
                "recent_purchases": ["P009"],
                "view_count_1h": 4,
                "offline_tags": {
                    "preferred_categories_30d": ["手机"],
                    "preferred_brands_30d": ["Huawei"],
                    "churn_risk_score": 0.2,
                },
            }

    engine = MemoryContextEngine(feature_store=StubFeatureStore())
    snapshot = asyncio.run(engine.build(user_id="u200", scene="homepage", context={}))

    assert snapshot.short_term.active_minutes_30m == 8
    assert snapshot.short_term.intent_categories[0] == "手机"
    assert snapshot.long_term.preferred_categories_30d == ["手机"]
    assert "high_churn_risk" not in snapshot.risk_flags
