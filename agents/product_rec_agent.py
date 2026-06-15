"""
ProductRecAgent 结构图

输入：
  user_profile + context + execution_plan + num_items + 可选 candidates
      |
      v
候选来源
  +-- 有 candidates：直接进入重排
  +-- 无 candidates：从 ProductRepository 商品库召回
      |
      v
_recall()
  +-- retrieve_strategy 决定候选怎么来
  +-- 目标：尽量别漏掉可能相关商品
      |
      v
_rerank()
  +-- rerank_focus 决定候选内部怎么排
  +-- 目标：把最合适的商品排到前面
      |
      v
ProductRecResult.products

一、输入

核心输入：
- `user_profile`：用户画像，主要提供 `preferred_categories`、`price_range`、`segments`
- `context`：请求上下文，里面会带 `user_context`、`execution_plan`、`ab_config`、`trigger_event`、`current_item`
- `execution_plan`：真正控制召回和重排策略的计划对象
- `num_items`：最终返回商品数
- `candidates`：如果上游已给候选，则跳过召回，直接重排

二、输出

主输出：
- `ProductRecResult.products`：最终排序后的商品列表

辅助输出：
- `data.candidate_count`：候选池大小
- `data.candidate_product_ids`：候选商品 ID
- `data.returned_product_ids`：最终返回商品 ID
- `data.rerank_strategy`：本次实际使用的重排方式
- `data.llm_called`：是否调用了 LLM
- `data.fallback`：是否走了兜底

三、召回做什么

召回解决的问题是“哪些商品应该进入候选池”。它优先追求覆盖面，不追求第一步就排出最终顺序。

召回策略与因素层级：

1. `semantic_first` / `hybrid`
   - 第一层：语义相关性
   - 第二层：命中偏好类目
   - 第三层：是否有库存
   - 说明：query 会综合近期行为、意图类目、长期偏好类目、当前商品信息

2. `inventory_first`
   - 第一层：库存量 `stock`
   - 第二层：命中偏好类目
   - 第三层：是否有库存

3. `hot_first`
   - 第一层：是否命中热门标签
   - 第二层：商品基础分 `score`
   - 第三层：库存量 `stock`
   - 第四层：命中偏好类目

说明：
- 这里没有统一数值权重，更准确地说是“优先级层级”
- 召回关心的是“找谁”，所以标准更粗

四、重排做什么

重排解决的问题是“候选里谁应该排前面”。它不会扩大候选池，只决定候选内部顺序。

本地规则重排的因素层级：

1. `price_first`
   - 第一层：价格更低优先

2. `brand_first`
   - 第一层：是否命中用户偏好品牌
   - 第二层：价格更低优先

3. `diversity`
   - 第一层：按类目拉开分布
   - 说明：这是轻量多样性偏置，不是复杂多样性模型

4. `balanced` / `intent_match`
   - 本地规则不强行改很多顺序
   - 更多交给 LLM 综合判断

LLM 重排参考因素：
- 用户兴趣和意图匹配
- 偏好类目
- 价格区间适配
- `user_context`
- `execution_plan`
- RAG 检索片段
- 候选商品的 `category`、`price`、`brand`、`stock`、`score`、`tags`

从当前 prompt 看，LLM 更像按下面层级综合判断：
- 第一层：兴趣 / 意图匹配
- 第二层：价格适配与多样性
- 第三层：库存、品牌、标签和基础属性

五、召回和重排的区别

- 召回关注覆盖面，宁可多捞一些候选，也不要漏掉可能相关商品
- 重排关注排序质量，要把最合适的商品排到前面
- 召回标准更粗，重排标准更细，也更依赖用户画像和策略计划

六、兜底

- LLM 只输出候选商品 ID 的顺序，不生成新商品
- 如果 LLM 不可用或输出不可解析，就按当前候选顺序返回前 N 个
"""


from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config import get_settings
from models.schemas import Product, ProductRecResult, UserProfile
from services.product_repository import (
    BASE_SEED_PRODUCTS,
    ProductRepository,
    get_product_repository,
)
from services.rag_vector_search import (
    InMemoryVectorIndex,
    ModernRAGRetriever,
    OpenAIEmbeddingProvider,
    build_product_docs,
)

from .base_agent import BaseAgent

RERANK_PROMPT = """You are an e-commerce reranking expert. Re-rank candidate products and return top items.

User profile:
{user_profile}

Candidates:
{candidates}

Requirements:
1. Match user interests and intent.
2. Consider price suitability and diversity.
3. Prefer in-stock and relevant items.

Output format (STRICT):
Return ONLY a JSON array of product_id strings, e.g. ["P001","P004","P002"].
Do NOT return markdown, code fences, explanations, or object wrappers.
"""

# 为单元测试保留的兼容数据；运行时召回会通过 ProductRepository 读取数据库。
MOCK_PRODUCTS = [p.model_copy(deep=True) for p in BASE_SEED_PRODUCTS]
RERANK_REPAIR_MAX_ATTEMPTS = 3


class ProductRecAgent(BaseAgent):
    def __init__(self):
        settings = get_settings()
        super().__init__(
            name="product_rec",
            timeout=settings.agent_timeout_product_rec,
        )
        self.llm: ChatOpenAI | None = None
        if settings.llm_api_key:
            self.llm = ChatOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                temperature=0.3,
                max_completion_tokens=2048,
                extra_body={"reasoning_split": True},
            )
        self.vector_store: Any = None  # 第二阶段可注入的向量存储
        self.settings = settings
        self.product_repository: ProductRepository = get_product_repository(settings.database_url)
        if settings.product_auto_seed:
            self.product_repository.seed_if_empty(settings.product_seed_count)
        self._catalog_cache: list[Product] = []
        self._catalog_count = -1
        self.rag_index = InMemoryVectorIndex(build_product_docs(self._catalog_products()))
        self.modern_rag: ModernRAGRetriever | None = None
        self._modern_rag_ready = False

    async def _execute(self, **kwargs: Any) -> ProductRecResult:
        user_profile: UserProfile | None = kwargs.get("user_profile")
        num_items: int = kwargs.get("num_items", 10)
        context: dict[str, Any] = kwargs.get("context", {})
        provided_candidates: list[Product] = kwargs.get("candidates", [])

        if provided_candidates:
            candidates = provided_candidates[: max(num_items * 3, num_items)]
        else:
            candidates = await self._recall(user_profile, context, num_items * 3)
        retrieved_docs = self._retrieve_docs_for_prompt(user_profile, context, top_k=5)
        ranked_ids, rerank_meta = await self._rerank(
            user_profile, context, candidates, num_items, retrieved_docs
        )

        id_to_product = {p.product_id: p for p in candidates}
        final_products = []
        for pid in ranked_ids:
            if pid in id_to_product:
                final_products.append(id_to_product[pid])
        if len(final_products) < num_items:
            for p in candidates:
                if p.product_id not in ranked_ids:
                    final_products.append(p)
                    if len(final_products) >= num_items:
                        break

        return ProductRecResult(
            success=True,
            products=final_products[:num_items],
            recall_strategy="collaborative_filter+vector+hot",
            data={
                "product_source": "database",
                "database_url": self.product_repository.safe_database_url,
                "catalog_count": self._catalog_count,
                "candidate_count": len(candidates),
                "candidate_product_ids": [p.product_id for p in candidates],
                "reranked": len(ranked_ids),
                "returned_product_ids": [p.product_id for p in final_products[:num_items]],
                "rerank_strategy": str(rerank_meta.get("rerank_strategy", "")),
                "llm_called": bool(rerank_meta.get("llm_called", False)),
                "fallback": bool(rerank_meta.get("fallback", False)),
                "fallback_reason": str(rerank_meta.get("fallback_reason", "")),
                "llm_parse_ok": bool(rerank_meta.get("llm_parse_ok", False)),
                "raw_response": str(rerank_meta.get("raw_response", "")),
                "retry_used": bool(rerank_meta.get("retry_used", False)),
                "retry_attempts": int(rerank_meta.get("retry_attempts", 0)),
                "retry_succeeded": bool(rerank_meta.get("retry_succeeded", False)),
            },
            confidence=0.8,
        )

    async def _recall(
        self, profile: UserProfile | None, context: dict[str, Any], limit: int
    ) -> list[Product]:
        """多策略召回：语义检索、库存优先、热门优先和规则偏好混合。"""
        plan = self._extract_plan(context)
        retrieve_strategy = str(plan.get("retrieve_strategy", "hybrid"))
        candidates = list(self._catalog_products())
        use_vector = retrieve_strategy in {"semantic_first", "hybrid"}
        vector_candidates = self._vector_recall(profile, context, limit) if use_vector else []
        if vector_candidates:
            candidates = self._merge_candidates(vector_candidates, candidates)
        elif retrieve_strategy == "inventory_first":
            candidates.sort(key=lambda p: p.stock, reverse=True)
        elif retrieve_strategy == "hot_first":
            hot_tags = {"热门", "爆款", "旗舰", "新品", "游戏", "gaming", "mobile", "hot"}
            candidates.sort(
                key=lambda p: (bool(set(p.tags) & hot_tags), p.score, p.stock),
                reverse=True,
            )

        preferred_from_profile = profile.preferred_categories if profile else []
        preferred_from_context = self._context_preferred_categories(context)
        preferred = set(preferred_from_profile + preferred_from_context)
        if preferred:
            candidates.sort(
                key=lambda p: (p.category in preferred, p.stock > 0, -self._product_sequence(p.product_id)),
                reverse=True,
            )

        return candidates[:limit]

    def _vector_recall(
        self, profile: UserProfile | None, context: dict[str, Any], limit: int
    ) -> list[Product]:
        query = self._build_retrieval_query(profile, context)
        if not query:
            return []
        hits = self._search_rag_hits(query, limit=max(limit, self.settings.rag_top_k))
        id_to_product = {p.product_id: p for p in self._catalog_products()}
        result: list[Product] = []
        for doc, score in hits:
            if score < 0:
                continue
            pid = str(doc.metadata.get("product_id", ""))
            if pid and pid in id_to_product:
                result.append(id_to_product[pid])
        return result

    def _search_rag_hits(
        self, query: str, limit: int
    ) -> list[tuple[Any, float]]:
        if self.settings.rag_enabled and not self._modern_rag_ready:
            self.modern_rag = self._build_modern_rag()
            self._modern_rag_ready = True
        if self.settings.rag_enabled and self.modern_rag:
            try:
                return self.modern_rag.search(query, top_k=limit)
            except Exception:
                # 语义检索失败时回退到本地轻量检索，保证服务稳定。
                pass
        return self.rag_index.search(query, top_k=limit)

    def _build_modern_rag(self) -> ModernRAGRetriever | None:
        if not self.settings.rag_enabled:
            return None
        if not self.settings.llm_api_key:
            return None
        if self.settings.rag_backend == "lexical":
            return None
        try:
            provider = OpenAIEmbeddingProvider(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                model=self.settings.embedding_model,
            )
            backend = self.settings.rag_backend
            if backend == "numpy":
                backend = "auto"
            return ModernRAGRetriever(
                docs=build_product_docs(self._catalog_products()),
                provider=provider,
                backend=backend,
            )
        except Exception:
            return None

    def _build_retrieval_query(
        self, profile: UserProfile | None, context: dict[str, Any]
    ) -> str:
        chunks: list[str] = []
        user_context = context.get("user_context", {})
        if isinstance(user_context, dict):
            short_term = user_context.get("short_term", {})
            long_term = user_context.get("long_term", {})
            intent = user_context.get("intent", {})
            chunks.extend(short_term.get("recent_views_1h", []))
            chunks.extend(short_term.get("intent_categories", []))
            chunks.extend(long_term.get("preferred_categories_30d", []))
            chunks.extend(intent.get("focus_categories", []))

        chunks.extend(context.get("recent_views", []))
        trigger = context.get("trigger_event")
        if trigger:
            chunks.append(str(trigger))
        current_item = context.get("current_item", {})
        if isinstance(current_item, dict):
            chunks.append(str(current_item.get("name", "")))
            chunks.append(str(current_item.get("category", "")))

        if profile:
            chunks.extend(profile.preferred_categories)
        return " ".join(str(c) for c in chunks if c).strip()

    def _merge_candidates(
        self, first: list[Product], second: list[Product]
    ) -> list[Product]:
        merged: list[Product] = []
        seen: set[str] = set()
        for p in first + second:
            if p.product_id in seen:
                continue
            seen.add(p.product_id)
            merged.append(p)
        return merged

    async def _rerank(
        self,
        profile: UserProfile | None,
        context: dict[str, Any],
        candidates: list[Product],
        num_items: int,
        retrieved_docs: list[str] | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        configured_rerank_strategy = self._configured_ab_rerank_strategy(context)
        rerank_strategy = configured_rerank_strategy or "llm"
        if rerank_strategy == "rule_based":
            return self._rule_based_rerank(candidates, context, num_items)

        force_llm = configured_rerank_strategy == "llm"
        if not force_llm and not profile and not context.get("user_context"):
            return (
                [p.product_id for p in candidates[:num_items]],
                {
                    "rerank_strategy": rerank_strategy,
                    "llm_called": False,
                    "fallback": True,
                    "fallback_reason": "insufficient_profile_context",
                    "llm_parse_ok": False,
                    "retry_attempts": 0,
                },
            )

        if self.llm is None:
            return (
                [p.product_id for p in candidates[:num_items]],
                {
                    "rerank_strategy": rerank_strategy,
                    "llm_called": False,
                    "fallback": True,
                    "fallback_reason": "llm_api_key_not_configured",
                    "llm_parse_ok": False,
                    "retry_attempts": 0,
                },
            )

        profile_summary = {
            "segments": [s.value for s in profile.segments] if profile else [],
            "preferred_categories": profile.preferred_categories if profile else [],
            "price_range": list(profile.price_range) if profile else [0, 10000],
            "user_context": context.get("user_context", {}),
            "execution_plan": self._extract_plan(context),
            "retrieved_docs": retrieved_docs or [],
        }
        candidate_summary = [
            {
                "id": p.product_id,
                "name": p.name,
                "category": p.category,
                "price": p.price,
                "brand": p.brand,
                "stock": p.stock,
                "score": p.score,
                "tags": p.tags,
            }
            for p in candidates
        ]
        candidate_summary = self._apply_plan_rerank_bias(candidate_summary, context)
        prompt = RERANK_PROMPT.format(
            num_items=num_items,
            user_profile=json.dumps(profile_summary, ensure_ascii=False, default=str),
            candidates=json.dumps(candidate_summary, ensure_ascii=False),
        ) + (
            "\n\nSTRICT OUTPUT FORMAT:\n"
            'Return ONLY a JSON array like [""P001"",""P002""]\n'
            "No markdown, no code fences, no explanation, no extra keys."
        )
        messages = [
            SystemMessage(content="You are an e-commerce reranking assistant. Output JSON array only."),
            HumanMessage(content=prompt),
        ]
        raw_response = ""
        raw_outputs: list[tuple[str, str]] = []
        retry_used = False
        retry_attempts = 0
        try:
            response = await self.llm.ainvoke(messages)
            raw_response = str(response.content)
            raw_outputs.append(("initial", raw_response))
            allowed_ids = [str(p.product_id) for p in candidates]
            parsed = self._valid_rerank_ids(
                self._parse_rerank_ids(raw_response),
                allowed_ids=allowed_ids,
            )
            if not parsed:
                retry_used = True
                for attempt in range(1, RERANK_REPAIR_MAX_ATTEMPTS + 1):
                    retry_attempts = attempt
                    repair_messages = self._build_rerank_repair_messages(
                        attempt=attempt,
                        num_items=num_items,
                        allowed_ids=allowed_ids,
                        profile_summary=profile_summary,
                        candidate_summary=candidate_summary,
                        previous_output=raw_outputs[-1][1],
                    )
                    repair_response = await self.llm.ainvoke(repair_messages)
                    repair_raw = str(repair_response.content)
                    raw_outputs.append((f"retry_{attempt}", repair_raw))
                    parsed = self._valid_rerank_ids(
                        self._parse_rerank_ids(repair_raw),
                        allowed_ids=allowed_ids,
                    )
                    if parsed:
                        break
            raw_response = self._format_raw_rerank_outputs(raw_outputs)
            if not parsed:
                raise ValueError("rerank output parse empty")
            return (
                [str(x) for x in parsed],
                {
                    "rerank_strategy": rerank_strategy,
                    "llm_called": True,
                    "fallback": False,
                    "fallback_reason": "",
                    "llm_parse_ok": True,
                    "raw_response": raw_response,
                    "retry_used": retry_used,
                    "retry_attempts": retry_attempts,
                    "retry_succeeded": retry_used and retry_attempts > 0,
                },
            )
        except Exception as exc:
            if raw_outputs:
                raw_response = self._format_raw_rerank_outputs(raw_outputs)
            return (
                [p.product_id for p in candidates[:num_items]],
                {
                    "rerank_strategy": rerank_strategy,
                    "llm_called": True,
                    "fallback": True,
                    "fallback_reason": f"llm_rerank_error_or_parse_failure: {exc}",
                    "llm_parse_ok": False,
                    "raw_response": raw_response,
                    "retry_used": retry_used,
                    "retry_attempts": retry_attempts,
                    "retry_succeeded": False,
                },
            )

    def _rule_based_rerank(
        self,
        candidates: list[Product],
        context: dict[str, Any],
        num_items: int,
    ) -> tuple[list[str], dict[str, Any]]:
        candidate_summary = self._candidate_summary(candidates)
        biased = self._apply_plan_rerank_bias(candidate_summary, context)
        ranked_ids = [str(item.get("id", "")) for item in biased if item.get("id")]
        if len(ranked_ids) < len(candidates):
            seen = set(ranked_ids)
            ranked_ids.extend(p.product_id for p in candidates if p.product_id not in seen)
        return (
            ranked_ids[:num_items],
            {
                "rerank_strategy": "rule_based",
                "llm_called": False,
                "fallback": False,
                "fallback_reason": "",
                "llm_parse_ok": False,
                "raw_response": "",
                "retry_used": False,
                "retry_attempts": 0,
                "retry_succeeded": False,
            },
        )

    def _candidate_summary(self, candidates: list[Product]) -> list[dict[str, Any]]:
        return [
            {
                "id": p.product_id,
                "name": p.name,
                "category": p.category,
                "price": p.price,
                "brand": p.brand,
                "stock": p.stock,
                "score": p.score,
                "tags": p.tags,
            }
            for p in candidates
        ]

    @staticmethod
    def _configured_ab_rerank_strategy(context: dict[str, Any]) -> str | None:
        ab_config = context.get("ab_config", {})
        if isinstance(ab_config, dict):
            strategy = str(ab_config.get("rerank", "")).strip().lower()
            if strategy in {"rule_based", "llm"}:
                return strategy
        return None

    def _build_rerank_repair_messages(
        self,
        attempt: int,
        num_items: int,
        allowed_ids: list[str],
        profile_summary: dict[str, Any],
        candidate_summary: list[dict[str, Any]],
        previous_output: str,
    ) -> list[Any]:
        compact_candidates = [
            {
                "id": item.get("id"),
                "category": item.get("category"),
                "price": item.get("price"),
                "brand": item.get("brand"),
                "stock": item.get("stock"),
                "score": item.get("score"),
                "tags": item.get("tags", []),
            }
            for item in candidate_summary
        ]
        return [
            SystemMessage(
                content=(
                    "You are a strict JSON formatter for product reranking.\n"
                    "You have all required information in the user message.\n"
                    "Do not ask questions. Do not explain. Do not use markdown.\n"
                    "Return only a valid JSON array of product_id strings."
                )
            ),
            HumanMessage(
                content=(
                    f"Repair attempt: {attempt}/{RERANK_REPAIR_MAX_ATTEMPTS}\n"
                    f"Need exactly {max(1, min(num_items, len(allowed_ids)))} IDs, ordered best to worst.\n"
                    f"Allowed IDs: {json.dumps(allowed_ids, ensure_ascii=False)}\n"
                    f"User profile: {json.dumps(profile_summary, ensure_ascii=False, default=str)}\n"
                    f"Candidates: {json.dumps(compact_candidates, ensure_ascii=False, default=str)}\n"
                    f"Previous invalid output: {previous_output[:1200]}\n"
                    "Rules:\n"
                    "1. Use only IDs from Allowed IDs.\n"
                    "2. No duplicate IDs.\n"
                    "3. Output one JSON array only.\n"
                    '4. Example: ["P001","P003","P005"]'
                )
            ),
        ]

    @staticmethod
    def _valid_rerank_ids(ids: list[str], allowed_ids: list[str]) -> list[str]:
        allowed = set(allowed_ids)
        valid: list[str] = []
        seen: set[str] = set()
        for pid in ids:
            if pid not in allowed or pid in seen:
                continue
            seen.add(pid)
            valid.append(pid)
        return valid

    @staticmethod
    def _format_raw_rerank_outputs(outputs: list[tuple[str, str]]) -> str:
        return "\n\n".join(f"---{label}---\n{raw}" for label, raw in outputs)

    def _extract_plan(self, context: dict[str, Any]) -> dict[str, Any]:
        plan = context.get("execution_plan", {})
        return plan if isinstance(plan, dict) else {}

    def _apply_plan_rerank_bias(
        self, candidate_summary: list[dict[str, Any]], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        plan = self._extract_plan(context)
        rerank_focus = str(plan.get("rerank_focus", "balanced"))
        if rerank_focus == "price_first":
            return sorted(candidate_summary, key=lambda x: float(x.get("price", 0)))
        if rerank_focus == "brand_first":
            preferred_brands = (
                context.get("user_context", {})
                .get("long_term", {})
                .get("preferred_brands_30d", [])
            )
            preferred = {str(x) for x in preferred_brands}
            return sorted(
                candidate_summary,
                key=lambda x: (str(x.get("brand", "")) in preferred, -float(x.get("price", 0))),
                reverse=True,
            )
        if rerank_focus == "diversity":
            return sorted(candidate_summary, key=lambda x: str(x.get("category", "")))
        return candidate_summary

    def _context_preferred_categories(self, context: dict[str, Any]) -> list[str]:
        user_context = context.get("user_context", {})
        if not isinstance(user_context, dict):
            return []

        short_term = user_context.get("short_term", {})
        long_term = user_context.get("long_term", {})
        intent = user_context.get("intent", {})

        merged = []
        merged.extend(short_term.get("intent_categories", []))
        merged.extend(long_term.get("preferred_categories_30d", []))
        merged.extend(intent.get("focus_categories", []))
        return [str(x) for x in merged if x]

    def _retrieve_docs_for_prompt(
        self, profile: UserProfile | None, context: dict[str, Any], top_k: int = 5
    ) -> list[str]:
        query = self._build_retrieval_query(profile, context)
        if not query:
            return []
        hits = self._search_rag_hits(query, limit=top_k)
        snippets: list[str] = []
        for doc, score in hits:
            doc_id = str(getattr(doc, "doc_id", ""))
            doc_text = str(getattr(doc, "text", ""))
            snippets.append(f"[{doc_id}] score={score:.3f} {doc_text[:160]}")
        return snippets

    def _catalog_products(self) -> list[Product]:
        count = self.product_repository.count_products()
        if not self._catalog_cache or count != self._catalog_count:
            self._catalog_cache = self.product_repository.list_products(
                limit=self.settings.product_catalog_cache_limit,
                in_stock_only=False,
            )
            self._catalog_count = count
            self.rag_index = InMemoryVectorIndex(build_product_docs(self._catalog_cache))
            self.modern_rag = None
            self._modern_rag_ready = False
        return [p.model_copy(deep=True) for p in self._catalog_cache]

    def _product_sequence(self, product_id: str) -> int:
        match = re.search(r"(\d+)$", product_id)
        return int(match.group(1)) if match else 10**9

    def _parse_rerank_ids(self, raw: str) -> list[str]:
        cleaned = (raw or "").strip()
        if not cleaned:
            return []

        # 先移除显式思考块，避免干扰 JSON 解析。
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE).strip()

        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()

        # 1) 直接解析 JSON 格式的 ID 列表。
        ids = self._load_id_list(cleaned)
        if ids:
            return ids

        # 2) 从混合文本里提取 JSON 数组。
        # 从后往前尝试每个类似 JSON 数组的片段。
        arr_matches = list(re.finditer(r"\[[^\[\]]*\]", cleaned))
        for m in reversed(arr_matches):
            ids = self._load_id_list(m.group(0))
            if ids:
                return ids

        # 2.5) 兼容被截断的 JSON 数组尾部，例如：... ["P005","P003","P006","P"
        last_lb = cleaned.rfind("[")
        if last_lb >= 0:
            tail = cleaned[last_lb:]
            tail_ids = re.findall(r"P\d{3}", tail)
            if tail_ids:
                # 保留原有顺序并去重。
                uniq: list[str] = []
                seen: set[str] = set()
                for pid in tail_ids:
                    if pid in seen:
                        continue
                    seen.add(pid)
                    uniq.append(pid)
                if uniq:
                    return uniq

        # 3) 兼容 {"ranked_ids":[...]} 这类对象包装格式。
        obj_match = re.search(r"\{[\s\S]*\}", cleaned)
        if obj_match:
            try:
                obj = json.loads(obj_match.group(0))
                if isinstance(obj, dict):
                    for key in ("ranked_ids", "product_ids", "ids", "ranking"):
                        value = obj.get(key)
                        if isinstance(value, list):
                            normalized = [str(x).strip() for x in value if str(x).strip()]
                            if normalized:
                                return normalized
            except Exception:
                pass
        return []

    def _load_id_list(self, text: str) -> list[str]:
        try:
            parsed = json.loads(text)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        return []
