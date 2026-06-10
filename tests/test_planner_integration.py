import asyncio

from agents.planner_agent import PlannerAgent
from models.schemas import ExecutionPlan, PlannerResult, RecommendationRequest
from orchestrator.supervisor import SupervisorOrchestrator
from services.auto_planner import AutoPlanner


def test_auto_planner_includes_business_goal():
    planner = AutoPlanner()
    plan = asyncio.run(
        planner.plan(
            scene="detail",
            user_context={"risk_flags": [], "preference": {"price_sensitivity": 0.3}},
            request_context={"trigger_event": "detail_view"},
            business_goal="margin",
        )
    )
    assert plan.plan_version == "v1"
    assert plan.business_goal == "margin"
    assert plan.retrieve_strategy == "semantic_first"


def test_planner_agent_fallback_when_planner_times_out():
    class SlowPlanner:
        async def plan(self, **kwargs):
            await asyncio.sleep(0.05)
            return ExecutionPlan()

    agent = PlannerAgent(planner=SlowPlanner())
    agent.timeout = 0.001
    result = asyncio.run(
        agent.run(
            scene="homepage",
            user_context={},
            request_context={},
            business_goal="conversion",
        )
    )
    assert result.success is False
    assert result.plan_hit is False
    assert result.execution_plan.retrieve_strategy == "hybrid"


def test_supervisor_plan_payload_passthrough():
    class StubPlannerAgent:
        async def run(self, **kwargs):
            return PlannerResult(
                success=True,
                plan_hit=True,
                execution_plan=ExecutionPlan(
                    plan_version="v9",
                    retrieve_strategy="inventory_first",
                    rerank_focus="price_first",
                    copy_tone="promotion",
                    risk_policy="retention_boost",
                    business_goal="clearance",
                    metadata={"planner_mode": "test"},
                ),
            )

    class StubMemoryEngine:
        async def build(self, user_id: str, scene: str, context: dict):
            from models.schemas import UserContextSnapshot

            return UserContextSnapshot(user_id=user_id, scene=scene)

    class StubUserProfileAgent:
        async def run(self, user_id: str, context: dict):
            from models.schemas import UserProfileResult

            return UserProfileResult(profile=None)

    class StubProductRecAgent:
        def __init__(self):
            self.context_calls = []

        async def run(self, user_profile, context: dict, num_items: int):
            self.context_calls.append(context)
            from models.schemas import Product, ProductRecResult

            return ProductRecResult(
                products=[Product(product_id="P1", name="A", category="c", price=1.0)]
            )

    class StubInventoryAgent:
        async def run(self, products, context=None):
            from models.schemas import InventoryResult

            return InventoryResult(available_products=[p.product_id for p in products])

    class StubCopyAgent:
        async def run(self, user_profile, products, context=None):
            from models.schemas import MarketingCopyResult

            return MarketingCopyResult(copies=[{"product_id": "P1", "copy": "x"}])

    supervisor = SupervisorOrchestrator()
    supervisor.planner_agent = StubPlannerAgent()
    supervisor.memory_engine = StubMemoryEngine()
    supervisor.user_profile_agent = StubUserProfileAgent()
    supervisor.product_rec_agent = StubProductRecAgent()
    supervisor.inventory_agent = StubInventoryAgent()
    supervisor.marketing_copy_agent = StubCopyAgent()

    response = asyncio.run(
        supervisor.recommend(
            RecommendationRequest(
                user_id="u001", scene="homepage", business_goal="clearance", num_items=1
            )
        )
    )
    assert response.plan_version == "v9"
    assert response.plan_payload["rerank_focus"] == "price_first"
    assert response.plan_replay_key.startswith("u001:homepage:v9:")
    assert response.planner_observability["ab_group"] in {"control", "treatment_llm"}
    assert "latency_bucket" in response.planner_observability
    assert all("execution_plan" in c for c in supervisor.product_rec_agent.context_calls)
