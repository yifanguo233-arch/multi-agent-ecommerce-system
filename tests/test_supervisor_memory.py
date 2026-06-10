import asyncio

from models.schemas import (
    InventoryResult,
    MarketingCopyResult,
    Product,
    ProductRecResult,
    RecommendationRequest,
    UserProfile,
    UserProfileResult,
    UserSegment,
)
from orchestrator.supervisor import SupervisorOrchestrator


def test_supervisor_passes_context_and_plan_flags():
    class StubMemoryEngine:
        async def build(self, user_id: str, scene: str, context: dict):
            from models.schemas import UserContextSnapshot

            return UserContextSnapshot(
                user_id=user_id,
                scene=scene,
                short_term={"session_id": "sess-1", "intent_categories": ["gaming"]},
                long_term={"preferred_categories_30d": ["gaming"]},
                intent={"focus_categories": ["gaming"]},
                preference={"price_sensitivity": 0.6},
                risk_flags=[],
            )

    class StubUserProfileAgent:
        def __init__(self):
            self.last_context = {}

        async def run(self, user_id: str, context: dict):
            self.last_context = context
            return UserProfileResult(
                profile=UserProfile(
                    user_id=user_id,
                    segments=[UserSegment.ACTIVE],
                    preferred_categories=["gaming"],
                    price_range=(1000, 5000),
                )
            )

    class StubProductRecAgent:
        def __init__(self):
            self.context_calls = []
            self.products = [
                Product(product_id="P015", name="Switch 2", category="gaming", price=2499),
                Product(product_id="P003", name="AirPods Pro 3", category="audio", price=1899),
            ]

        async def run(self, user_profile, context: dict, num_items: int):
            self.context_calls.append(context)
            return ProductRecResult(products=self.products[:num_items])

    class StubInventoryAgent:
        async def run(self, products, context=None):
            return InventoryResult(available_products=[p.product_id for p in products])

    class StubCopyAgent:
        async def run(self, user_profile, products, context=None):
            return MarketingCopyResult(
                copies=[{"product_id": products[0].product_id, "copy": "test"}]
            )

    supervisor = SupervisorOrchestrator()
    supervisor.memory_engine = StubMemoryEngine()
    supervisor.user_profile_agent = StubUserProfileAgent()
    supervisor.product_rec_agent = StubProductRecAgent()
    supervisor.inventory_agent = StubInventoryAgent()
    supervisor.marketing_copy_agent = StubCopyAgent()

    request = RecommendationRequest(user_id="u001", scene="homepage", num_items=2, context={})
    response = asyncio.run(supervisor.recommend(request))

    assert response.context_version == "v1"
    assert response.memory_hit is True
    assert response.plan_version == "v1"
    assert response.plan_hit is True
    assert "user_context" in response.debug_context
    assert "execution_plan" in response.debug_context
    assert "plan_payload" in response.model_dump()
    assert isinstance(response.execution_plan, dict)
    assert response.products[0].category == "gaming"

    profile_ctx = supervisor.user_profile_agent.last_context
    assert "user_context" in profile_ctx
    assert "execution_plan" in profile_ctx
    assert profile_ctx["user_context"]["short_term"]["intent_categories"] == ["gaming"]

    assert len(supervisor.product_rec_agent.context_calls) == 2
    assert all("user_context" in c for c in supervisor.product_rec_agent.context_calls)
    assert all("execution_plan" in c for c in supervisor.product_rec_agent.context_calls)
