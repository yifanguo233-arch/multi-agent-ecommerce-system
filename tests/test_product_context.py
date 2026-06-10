import asyncio

from agents.product_rec_agent import ProductRecAgent


def test_context_preferred_categories_merge():
    agent = ProductRecAgent()
    context = {
        "user_context": {
            "short_term": {"intent_categories": ["游戏机", "耳机"]},
            "long_term": {"preferred_categories_30d": ["平板"]},
            "intent": {"focus_categories": ["显示器"]},
        }
    }

    categories = agent._context_preferred_categories(context)
    assert "游戏机" in categories
    assert "平板" in categories
    assert "显示器" in categories


def test_recall_is_influenced_by_user_context():
    agent = ProductRecAgent()
    context = {
        "user_context": {
            "short_term": {"intent_categories": ["游戏机"]},
            "long_term": {"preferred_categories_30d": []},
            "intent": {"focus_categories": []},
        }
    }

    candidates = asyncio.run(agent._recall(profile=None, context=context, limit=8))
    assert len(candidates) == 8
    assert candidates[0].category == "游戏机"
    assert candidates[0].product_id == "P015"
