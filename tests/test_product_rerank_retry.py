import asyncio

from agents.product_rec_agent import ProductRecAgent
from models.schemas import Product, UserProfile, UserSegment


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


def test_rerank_repairs_invalid_llm_output_with_multiple_requests():
    agent = ProductRecAgent()
    agent.llm = StubLLM(
        [
            "I need more information before ranking.",
            "Still not enough information.",
            "Please provide ranking criteria.",
            '["P002", "P001"]',
        ]
    )
    profile = UserProfile(
        user_id="u1",
        segments=[UserSegment.ACTIVE],
        preferred_categories=["手机"],
        price_range=(0, 10000),
    )
    candidates = [
        Product(product_id="P001", name="A", category="手机", price=999, stock=10),
        Product(product_id="P002", name="B", category="耳机", price=299, stock=10),
    ]

    ranked_ids, meta = asyncio.run(
        agent._rerank(
            profile=profile,
            context={},
            candidates=candidates,
            num_items=2,
            retrieved_docs=[],
        )
    )

    assert ranked_ids == ["P002", "P001"]
    assert agent.llm.calls == 4
    assert meta["retry_used"] is True
    assert meta["retry_attempts"] == 3
    assert meta["retry_succeeded"] is True
    assert meta["fallback"] is False


def test_rule_based_ab_rerank_does_not_call_llm():
    agent = ProductRecAgent()
    agent.llm = StubLLM(['["P001", "P002"]'])
    profile = UserProfile(
        user_id="u1",
        segments=[UserSegment.ACTIVE],
        preferred_categories=["audio"],
        price_range=(0, 10000),
    )
    candidates = [
        Product(product_id="P001", name="A", category="audio", price=999, stock=10),
        Product(product_id="P002", name="B", category="audio", price=299, stock=10),
    ]

    ranked_ids, meta = asyncio.run(
        agent._rerank(
            profile=profile,
            context={
                "ab_config": {"rerank": "rule_based"},
                "execution_plan": {"rerank_focus": "price_first"},
            },
            candidates=candidates,
            num_items=2,
            retrieved_docs=[],
        )
    )

    assert ranked_ids == ["P002", "P001"]
    assert agent.llm.calls == 0
    assert meta["rerank_strategy"] == "rule_based"
    assert meta["llm_called"] is False
    assert meta["fallback"] is False


def test_treatment_llm_ab_rerank_forces_llm_path():
    agent = ProductRecAgent()
    agent.llm = StubLLM(['["P002", "P001"]'])
    candidates = [
        Product(product_id="P001", name="A", category="audio", price=999, stock=10),
        Product(product_id="P002", name="B", category="audio", price=299, stock=10),
    ]

    ranked_ids, meta = asyncio.run(
        agent._rerank(
            profile=None,
            context={"ab_config": {"rerank": "llm"}},
            candidates=candidates,
            num_items=2,
            retrieved_docs=[],
        )
    )

    assert ranked_ids == ["P002", "P001"]
    assert agent.llm.calls == 1
    assert meta["rerank_strategy"] == "llm"
    assert meta["llm_called"] is True
    assert meta["fallback"] is False
