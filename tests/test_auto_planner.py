import asyncio

from services.auto_planner import AutoPlanner


def test_auto_planner_price_sensitive_and_churn():
    planner = AutoPlanner()
    user_context = {
        "risk_flags": ["high_price_sensitivity", "high_churn_risk"],
        "preference": {"price_sensitivity": 0.9},
    }
    plan = asyncio.run(
        planner.plan(
            scene="homepage",
            user_context=user_context,
            request_context={"trigger_event": "browse"},
        )
    )

    assert plan.plan_version == "v1"
    assert plan.rerank_focus == "price_first"
    assert plan.copy_tone == "reassure"
    assert plan.risk_policy == "retention_boost"
    assert plan.filters.get("diversity_boost") is True


def test_auto_planner_detail_scene_prefers_semantic_retrieval():
    planner = AutoPlanner()
    plan = asyncio.run(
        planner.plan(
            scene="detail",
            user_context={"risk_flags": [], "preference": {"price_sensitivity": 0.3}},
            request_context={"trigger_event": "detail_view"},
        )
    )
    assert plan.retrieve_strategy == "semantic_first"
    assert plan.rerank_focus == "intent_match"


def test_auto_planner_parses_minimax_reasoning_response():
    planner = AutoPlanner()
    raw = """<think>
The response should be conservative and stable.
</think>

```json
{"retrieve_strategy": "hot_first", "rerank_focus": "balanced", "copy_tone": "default", "risk_policy": "standard"}
```"""

    parsed = planner._parse_llm_json(raw)

    assert parsed == {
        "retrieve_strategy": "hot_first",
        "rerank_focus": "balanced",
        "copy_tone": "default",
        "risk_policy": "standard",
    }


def test_auto_planner_extracts_json_from_surrounding_text():
    planner = AutoPlanner()
    raw = 'Here is the plan:\n{"copy_tone": "promotion", "risk_policy": "stock_guard"}'

    parsed = planner._parse_llm_json(raw)

    assert parsed == {"copy_tone": "promotion", "risk_policy": "stock_guard"}


def test_auto_planner_extracts_plan_fields_from_reasoning_text():
    planner = AutoPlanner()
    raw = """<think>
- retrieve_strategy: "semantic_first" makes sense for detail pages
- rerank_focus: "balanced" is conservative
- copy_tone: "reassure" helps conversion
- risk_policy: "standard" is safest
</think>"""

    parsed = planner._parse_llm_json(raw)

    assert parsed == {
        "retrieve_strategy": "semantic_first",
        "rerank_focus": "balanced",
        "copy_tone": "reassure",
        "risk_policy": "standard",
    }
