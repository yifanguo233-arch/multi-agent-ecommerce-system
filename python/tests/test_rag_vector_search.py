import asyncio

from agents.product_rec_agent import MOCK_PRODUCTS, ProductRecAgent
from services.rag_vector_search import InMemoryVectorIndex, build_product_docs


def test_vector_index_search_returns_relevant_product():
    index = InMemoryVectorIndex(build_product_docs(MOCK_PRODUCTS))

    hits = index.search("游戏机 任天堂 Switch", top_k=3)
    assert len(hits) > 0
    top_doc = hits[0][0]
    assert top_doc.metadata["category"] == "游戏机"


def test_product_agent_vector_recall_uses_context_query():
    agent = ProductRecAgent()
    context = {
        "user_context": {
            "short_term": {"recent_views_1h": ["Switch"], "intent_categories": ["游戏机"]},
            "long_term": {"preferred_categories_30d": []},
            "intent": {"focus_categories": ["游戏机"]},
        }
    }
    recs = asyncio.run(agent._recall(profile=None, context=context, limit=5))
    assert len(recs) == 5
    assert recs[0].category == "游戏机"
