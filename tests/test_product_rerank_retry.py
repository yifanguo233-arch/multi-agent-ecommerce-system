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
