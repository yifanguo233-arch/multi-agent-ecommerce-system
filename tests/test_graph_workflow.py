import asyncio
import uuid
from pathlib import Path

from models.schemas import (
    ExecutionPlan,
    InventoryResult,
    MarketingCopyResult,
    Product,
    ProductRecResult,
    UserProfileResult,
)
from orchestrator import graph as graph_mod
from services.sqlite_checkpoint import GraphReplayIndexStore


def _checkpoint_path(name: str) -> str:
    root = Path("test_checkpoints_tmp")
    root.mkdir(exist_ok=True)
    return str(root / f"{name}-{uuid.uuid4().hex}.db")


def test_route_after_filter():
    assert graph_mod.route_after_filter({"final_products": []}) == "inventory_repair"
    assert (
        graph_mod.route_after_filter(
            {
                "final_products": [
                    Product(product_id="p1", name="n", category="c", price=1.0)
                ],
                "user_context": {"risk_flags": ["high_churn_risk"]},
            }
        )
        == "retention_offer"
    )
    assert (
        graph_mod.route_after_filter(
            {
                "final_products": [
                    Product(product_id="p1", name="n", category="c", price=1.0)
                ],
                "user_context": {"risk_flags": []},
            }
        )
        == "marketing_copy"
    )


def test_route_after_marketing_copy_force_and_low_diversity():
    assert (
        graph_mod.route_after_marketing_copy(
            {
                "max_reflections": 1,
                "reflection_count": 0,
                "context": {"force_reflection": True},
                "final_products": [
                    Product(product_id="p1", name="n", category="c1", price=1.0)
                ],
                "marketing_copies": [{"product_id": "p1", "copy": "ok"}],
            }
        )
        == "reflection"
    )
    assert (
        graph_mod.route_after_marketing_copy(
            {
                "max_reflections": 1,
                "reflection_count": 0,
                "context": {},
                "final_products": [
                    Product(product_id="p1", name="n1", category="same", price=1.0),
                    Product(product_id="p2", name="n2", category="same", price=2.0),
                ],
                "marketing_copies": [
                    {"product_id": "p1", "copy": "ok"},
                    {"product_id": "p2", "copy": "ok"},
                ],
            }
        )
        == "reflection"
    )


def test_graph_pipeline_contains_plan_and_trace():
    class StubMemory:
        async def build(self, user_id: str, scene: str, context: dict):
            class Snapshot:
                def model_dump(self, *args, **kwargs):
                    return {"risk_flags": [], "short_term": {}, "long_term": {}, "intent": {}}

            return Snapshot()

    class StubPlanner:
        async def run(self, **kwargs):
            from models.schemas import PlannerResult

            return PlannerResult(
                success=True,
                plan_hit=True,
                execution_plan=ExecutionPlan(
                    plan_version="v2",
                    retrieve_strategy="hybrid",
                    rerank_focus="balanced",
                    copy_tone="default",
                    risk_policy="standard",
                    business_goal="conversion",
                ),
            )

    class StubProfile:
        async def run(self, **kwargs):
            return UserProfileResult(profile=None)

    class StubRec:
        async def run(self, **kwargs):
            return ProductRecResult(
                products=[Product(product_id="p1", name="n", category="c", price=1.0)]
            )

    class StubInv:
        async def run(self, **kwargs):
            return InventoryResult(available_products=["p1"])

    class StubCopy:
        async def run(self, **kwargs):
            return MarketingCopyResult(copies=[{"product_id": "p1", "copy": "ok"}])

    graph_mod.memory_engine = StubMemory()
    graph_mod.planner_agent = StubPlanner()
    graph_mod.user_profile_agent = StubProfile()
    graph_mod.product_rec_agent = StubRec()
    graph_mod.inventory_agent = StubInv()
    graph_mod.marketing_copy_agent = StubCopy()

    thread_id = "thread-plan-trace-001"

    async def run_case():
        async with graph_mod.recommendation_graph_context(_checkpoint_path("plan-trace")) as app:
            return await app.ainvoke(
                {
                    "request_id": thread_id,
                    "user_id": "u001",
                    "scene": "homepage",
                    "num_items": 1,
                    "business_goal": "conversion",
                    "context": {},
                },
                config={"configurable": {"thread_id": thread_id}},
            )

    result = asyncio.run(run_case())

    assert result["plan_version"] == "v2"
    assert result["plan_payload"]["plan_version"] == "v2"
    assert isinstance(result["trace_steps"], list)
    assert "planner" in {x.get("node") for x in result["trace_steps"]}


def test_graph_checkpoint_replay_state():
    thread_id = "thread-test-001"
    state = {
        "request_id": thread_id,
        "user_id": "u001",
        "scene": "homepage",
        "num_items": 1,
        "business_goal": "conversion",
        "context": {},
    }

    async def run_case():
        async with graph_mod.recommendation_graph_context(_checkpoint_path("replay")) as app:
            await app.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
            return await app.aget_state({"configurable": {"thread_id": thread_id}})

    snap = asyncio.run(run_case())
    values = getattr(snap, "values", {})
    assert values.get("request_id") == thread_id
    assert "trace_steps" in values


def test_graph_checkpoint_replay_survives_graph_rebuild():
    checkpoint_path = _checkpoint_path("rebuild")
    thread_id = "thread-persist-001"
    state = {
        "request_id": thread_id,
        "user_id": "u001",
        "scene": "homepage",
        "num_items": 1,
        "business_goal": "conversion",
        "context": {},
    }

    async def run_first_graph():
        async with graph_mod.recommendation_graph_context(checkpoint_path) as app:
            await app.ainvoke(state, config={"configurable": {"thread_id": thread_id}})

    async def run_reloaded_graph():
        async with graph_mod.recommendation_graph_context(checkpoint_path) as app:
            return await app.aget_state({"configurable": {"thread_id": thread_id}})

    asyncio.run(run_first_graph())
    snap = asyncio.run(run_reloaded_graph())
    values = getattr(snap, "values", {})
    assert values.get("request_id") == thread_id
    assert "trace_steps" in values


def test_graph_replay_index_is_persistent():
    checkpoint_path = _checkpoint_path("index")
    GraphReplayIndexStore(checkpoint_path).remember("request-001", "thread-001")
    assert GraphReplayIndexStore(checkpoint_path).resolve("request-001") == "thread-001"


def test_graph_tool_registry_outputs_present():
    thread_id = "thread-tool-001"
    state = {
        "request_id": thread_id,
        "user_id": "u002",
        "scene": "homepage",
        "num_items": 1,
        "business_goal": "conversion",
        "context": {"reflection_hint": "too_few_products"},
    }

    async def run_case():
        async with graph_mod.recommendation_graph_context(_checkpoint_path("tools")) as app:
            return await app.ainvoke(state, config={"configurable": {"thread_id": thread_id}})

    result = asyncio.run(run_case())
    assert isinstance(result.get("selected_tools", []), list)
    assert isinstance(result.get("tool_outputs", {}), dict)
