from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np
from langchain_openai import OpenAIEmbeddings

from models.schemas import Product


@dataclass
class KnowledgeDoc:
    """RAG 检索单元：把一个商品或知识片段封装成可检索文档。"""

    doc_id: str
    text: str
    metadata: dict[str, Any]


class InMemoryVectorIndex:
    """轻量级内存检索索引：用词频向量做本地 RAG 召回。"""

    def __init__(self, docs: list[KnowledgeDoc]):
        """接收知识文档列表，并在初始化时构建词表和文档向量矩阵。"""
        self.docs = docs
        self.vocab: dict[str, int] = {}
        self.doc_vectors: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._build()

    def _build(self) -> None:
        """把所有文档分词、建立词表，并生成归一化后的词频向量矩阵。"""
        tokenized = [self._tokenize(doc.text) for doc in self.docs]
        vocab = {}
        for tokens in tokenized:  # 为每个不重复 token 分配一个整数 ID。
            for tok in tokens:
                if tok not in vocab:
                    vocab[tok] = len(vocab)
        self.vocab = vocab

        if not self.docs or not self.vocab:
            self.doc_vectors = np.zeros((len(self.docs), 0), dtype=np.float32)
            return

        mat = np.zeros((len(self.docs), len(self.vocab)), dtype=np.float32)  # 构建词频矩阵。
        for i, tokens in enumerate(tokenized):
            counts = Counter(tokens)
            for tok, cnt in counts.items():
                mat[i, self.vocab[tok]] = float(cnt)
        self.doc_vectors = self._l2_normalize(mat)

    def search(self, query: str, top_k: int = 10) -> list[tuple[KnowledgeDoc, float]]:
        """用 query 检索最相关的 top_k 篇文档，返回文档和相似度分数。"""
        if not self.docs:
            return []
        q = self._embed(query)
        if q.size == 0:
            return [(doc, 0.0) for doc in self.docs[:top_k]]

        scores = self.doc_vectors @ q
        idx = np.argsort(-scores)[:top_k]
        return [(self.docs[int(i)], float(scores[int(i)])) for i in idx]

    def _embed(self, text: str) -> np.ndarray:
        """把输入文本转成和文档词表同维度的词频向量。"""
        if not self.vocab:
            return np.zeros((0,), dtype=np.float32)
        vec = np.zeros((len(self.vocab),), dtype=np.float32)
        counts = Counter(self._tokenize(text))
        for tok, cnt in counts.items():
            j = self.vocab.get(tok)
            if j is not None:
                vec[j] = float(cnt)
        return self._l2_normalize(vec)

    @staticmethod
    def _l2_normalize(x: np.ndarray) -> np.ndarray:
        """对向量或矩阵做 L2 归一化，方便后续用点积表示余弦相似度。"""
        if x.ndim == 1:
            norm = float(np.linalg.norm(x))
            return x if norm == 0 else (x / norm)
        norms = np.linalg.norm(x, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return x / norms

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """把中英文混合文本切成 token，英文按词切，中文按单字补充。"""
        cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
        words = [w for w in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", cleaned) if w]
        cjk_chars = [ch for ch in cleaned if "\u4e00" <= ch <= "\u9fff"]
        # 同时保留英文词 token 和中文单字 token，提升中英文混合检索的鲁棒性。
        return words + cjk_chars


class EmbeddingProvider:
    """Embedding 提供者接口：屏蔽具体向量模型的调用方式。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """把多篇文档批量转成向量，子类需要实现具体模型调用。"""
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        """把单条查询文本转成向量，子类需要实现具体模型调用。"""
        raise NotImplementedError


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI 兼容的 EmbeddingProvider 实现，用于调用外部 embedding 模型。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        """根据 api_key、base_url 和模型名初始化 LangChain OpenAIEmbeddings 客户端。"""
        self.client = OpenAIEmbeddings(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """调用 embedding 服务，把文档列表转成稠密向量列表。"""
        return self.client.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        """调用 embedding 服务，把查询文本转成稠密向量。"""
        return self.client.embed_query(text)


class NumpyVectorStore:
    """基于 NumPy 的稠密向量库：用余弦相似度完成向量检索。"""

    def __init__(self, vectors: np.ndarray):
        """接收文档向量矩阵，并预先做 L2 归一化。"""
        self.vectors = self._l2_normalize(vectors.astype(np.float32))

    def search(self, query: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """用查询向量检索 top_k 个最相似文档，返回文档下标和分数。"""
        if self.vectors.size == 0:
            return []
        q = self._l2_normalize(query.astype(np.float32))
        scores = self.vectors @ q
        idx = np.argsort(-scores)[:top_k]
        return [(int(i), float(scores[int(i)])) for i in idx]

    @staticmethod
    def _l2_normalize(x: np.ndarray) -> np.ndarray:
        """对向量或矩阵做 L2 归一化，避免长度影响相似度计算。"""
        if x.ndim == 1:
            norm = float(np.linalg.norm(x))
            return x if norm == 0 else (x / norm)
        norms = np.linalg.norm(x, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return x / norms


class FaissVectorStore:
    """基于 FAISS 的向量库：可用时提供更高性能的向量检索后端。"""

    def __init__(self, vectors: np.ndarray):
        """初始化 FAISS 内积索引；如果环境没有 faiss，则抛错让上层降级。"""
        try:
            import faiss  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("faiss not available") from exc

        vec = vectors.astype(np.float32)
        dim = vec.shape[1] if vec.ndim == 2 and vec.shape[0] > 0 else 0
        self.faiss = faiss
        self.index = faiss.IndexFlatIP(dim)
        if dim > 0 and vec.shape[0] > 0:
            faiss.normalize_L2(vec)
            self.index.add(vec)

    def search(self, query: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """用 FAISS 检索 top_k 个最相似文档，返回文档下标和分数。"""
        if self.index.ntotal == 0:
            return []
        q = query.astype(np.float32).reshape(1, -1)
        self.faiss.normalize_L2(q)
        scores, idx = self.index.search(q, top_k)
        out: list[tuple[int, float]] = []
        for i, score in zip(idx[0], scores[0]):
            if i < 0:
                continue
            out.append((int(i), float(score)))
        return out


class ModernRAGRetriever:
    """现代 RAG 检索器：用 embedding 模型编码文本，再用 FAISS 或 NumPy 检索。"""

    def __init__(
        self,
        docs: list[KnowledgeDoc],
        provider: EmbeddingProvider,
        backend: str = "auto",
    ):
        """保存文档、embedding provider 和后端配置，并构建底层向量库。"""
        self.docs = docs
        self.provider = provider
        self.backend = backend
        self.store: NumpyVectorStore | FaissVectorStore | None = None
        self._build()

    def _build(self) -> None:
        """把所有文档转成 embedding 向量，并按配置选择 FAISS 或 NumPy 后端。"""
        if not self.docs:
            self.store = NumpyVectorStore(np.zeros((0, 0), dtype=np.float32))
            return
        texts = [d.text for d in self.docs]
        vectors = np.array(self.provider.embed_documents(texts), dtype=np.float32)
        if vectors.ndim != 2:
            vectors = vectors.reshape(len(self.docs), -1)

        if self.backend in ("faiss", "auto"):
            try:
                self.store = FaissVectorStore(vectors)
                return
            except Exception:
                if self.backend == "faiss":
                    raise
        self.store = NumpyVectorStore(vectors)

    def search(self, query: str, top_k: int = 10) -> list[tuple[KnowledgeDoc, float]]:
        """把 query 编码为向量并检索相关文档，返回文档对象和相似度分数。"""
        if not self.docs or not self.store:
            return []
        q = np.array(self.provider.embed_query(query), dtype=np.float32)
        if q.ndim != 1:
            q = q.reshape(-1)
        hits = self.store.search(q, top_k=top_k)
        return [(self.docs[i], score) for i, score in hits]


def build_product_docs(products: list[Product]) -> list[KnowledgeDoc]:
    """把商品列表转换成 RAG 文档，供召回和重排提示词补充上下文使用。"""
    docs: list[KnowledgeDoc] = []
    for p in products:
        text = " ".join(
            [
                p.name,
                p.category,
                p.brand,
                p.description,
                " ".join(p.tags),
            ]
        ).strip()
        docs.append(
            KnowledgeDoc(
                doc_id=p.product_id,
                text=text,
                metadata={"product_id": p.product_id, "category": p.category},
            )
        )
    return docs
