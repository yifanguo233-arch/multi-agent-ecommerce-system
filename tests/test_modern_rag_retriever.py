from services.rag_vector_search import (
    EmbeddingProvider,
    ModernRAGRetriever,
    build_product_docs,
)
from agents.product_rec_agent import MOCK_PRODUCTS


class FakeEmbeddingProvider(EmbeddingProvider):
    def _vec(self, text: str) -> list[float]:
        s = text.lower()
        # demo 用的语义轴：game、phone、audio
        return [
            1.0 if ("switch" in s or "游戏" in s or "任天堂" in s) else 0.0,
            1.0 if ("iphone" in s or "手机" in s) else 0.0,
            1.0 if ("耳机" in s or "airpods" in s) else 0.0,
        ]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def test_modern_rag_retriever_ranks_semantic_match_first():
    retriever = ModernRAGRetriever(
        docs=build_product_docs(MOCK_PRODUCTS),
        provider=FakeEmbeddingProvider(),
        backend="numpy",
    )
    hits = retriever.search("任天堂 游戏机 Switch", top_k=3)
    assert len(hits) > 0
    top_ids = [doc.doc_id for doc, _ in hits]
    assert "P015" in top_ids
