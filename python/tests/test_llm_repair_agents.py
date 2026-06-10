import asyncio

from agents.marketing_copy_agent import MarketingCopyAgent
from agents.user_profile_agent import UserProfileAgent
from models.schemas import Product, UserProfile, UserSegment
from services.auto_planner import AutoPlanner


class StubLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1

        class Response:
            def __init__(self, content):
                self.content = content

        return Response(self.responses.pop(0))


def test_auto_planner_repairs_invalid_plan_json():
    planner = AutoPlanner()
    planner.use_llm = True
    planner.llm_timeout = 5
    planner.llm = StubLLM(
        [
            "I would choose a promotional strategy.",
            '{"rerank_focus": "price_first", "copy_tone": "promotion"}',
        ]
    )

    plan = asyncio.run(
        planner.plan(
            scene="homepage",
            user_context={},
            request_context={},
            business_goal="conversion",
        )
    )

    assert planner.llm.calls == 2
    assert plan.rerank_focus == "price_first"
    assert plan.copy_tone == "promotion"
    assert plan.metadata["retry_used"] is True
    assert plan.metadata["retry_attempts"] == 1
    assert plan.metadata["retry_succeeded"] is True


def test_user_profile_agent_repairs_invalid_profile_json():
    agent = UserProfileAgent()
    agent.llm = StubLLM(
        [
            "This user seems price sensitive.",
            (
                '{"segments":["price_sensitive"],'
                '"preferred_categories":["手机"],'
                '"price_range":[100,1500],'
                '"rfm_score":{"recency":1,"frequency":0.1,"monetary":0.4},'
                '"real_time_tags":{"active_period":"evening"}}'
            ),
        ]
    )

    result = asyncio.run(agent._execute(user_id="u1", context={"recent_views": ["手机"]}))

    assert agent.llm.calls == 2
    assert result.profile.segments == [UserSegment.PRICE_SENSITIVE]
    assert result.data["retry_used"] is True
    assert result.data["retry_attempts"] == 1
    assert result.data["retry_succeeded"] is True
    assert result.data["llm_parse_ok"] is True


def test_marketing_copy_agent_repairs_invalid_copy_json():
    agent = MarketingCopyAgent()
    agent.llm = StubLLM(
        [
            "Here are some nice copies.",
            (
                '['
                '{"product_id":"P001","copy":"iPhone deal for you"},'
                '{"product_id":"P002","copy":"AirPods match your interest"}'
                ']'
            ),
        ]
    )
    profile = UserProfile(
        user_id="u1",
        segments=[UserSegment.ACTIVE],
        preferred_categories=["手机"],
    )
    products = [
        Product(product_id="P001", name="iPhone", category="手机", price=7999),
        Product(product_id="P002", name="AirPods", category="耳机", price=1899),
    ]

    result = asyncio.run(agent._execute(user_profile=profile, products=products, context={}))

    assert agent.llm.calls == 2
    assert [item["product_id"] for item in result.copies] == ["P001", "P002"]
    assert result.data["retry_used"] is True
    assert result.data["retry_attempts"] == 1
    assert result.data["retry_succeeded"] is True
    assert result.data["llm_parse_ok"] is True
