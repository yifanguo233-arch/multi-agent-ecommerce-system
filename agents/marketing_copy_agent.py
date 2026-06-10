"""
MarketingCopyAgent 结构图

输入：
  user_profile + products + context + execution_plan.copy_tone
      |
      v
选择文案策略
  +-- user_profile.segments -> 选择分群模板
  +-- request context.copy_tone 优先
  +-- 否则读取 execution_plan.copy_tone
      |
      v
LLM 生成 JSON 文案数组
      |
      +-- 成功：解析 copies，并做禁用词合规替换
      +-- 调用失败 / 解析为空：_rule_based_copies() 规则文案兜底

输出去向：
  MarketingCopyResult.copies
      -> Graph 聚合为 marketing_copies，最终返回给接口调用方

LLM 与兜底：
  LLM 负责生成个性化文案；失败时用商品名、品牌、价格和标签生成保守文案。

核心分支：
  用户分群决定模板；copy_tone 决定理性、促销、新品、安抚等语气。

当前边界：
  合规处理是简单禁用词替换，不是完整广告法审核系统。
  规则兜底文案偏保守，主要保证链路不断。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config import get_settings
from models.schemas import (
    MarketingCopyResult,
    Product,
    UserProfile,
    UserSegment,
)

from .base_agent import BaseAgent

PROMPT_TEMPLATES = {
    UserSegment.NEW_USER: """你是电商营销文案专家。为新用户撰写欢迎+推荐文案。
风格要求：热情友好、突出新人专属优惠感、降低决策门槛。
每个商品生成一条文案(30-50字)。""",

    UserSegment.HIGH_VALUE: """你是电商营销文案专家。为高价值VIP用户撰写推荐文案。
风格要求：品质感、尊享感、突出商品高端属性和品牌价值。
每个商品生成一条文案(30-50字)。""",

    UserSegment.PRICE_SENSITIVE: """你是电商营销文案专家。为价格敏感用户撰写推荐文案。
风格要求：突出性价比、促销价格、限时优惠、省钱金额。
每个商品生成一条文案(30-50字)。""",

    UserSegment.ACTIVE: """你是电商营销文案专家。为活跃用户撰写推荐文案。
风格要求：突出商品亮点和使用场景,引发共鸣。
每个商品生成一条文案(30-50字)。""",

    UserSegment.CHURN_RISK: """你是电商营销文案专家。为即将流失的用户撰写召回文案。
风格要求：情感唤回、专属折扣、限时活动、制造紧迫感。
每个商品生成一条文案(30-50字)。""",
}

FORBIDDEN_WORDS = [
    "最好", "第一", "国家级", "全球首", "绝对", "100%",
    "永久", "万能", "祖传", "纯天然",
]

COPY_OUTPUT_INSTRUCTION = """
请以JSON数组格式输出,每个元素格式:
[{"product_id": "xxx", "copy": "文案内容"}]
只输出JSON,不要其他内容。"""


COPY_REPAIR_MAX_ATTEMPTS = 2


class MarketingCopyAgent(BaseAgent):
    def __init__(self):
        settings = get_settings()
        super().__init__(
            name="marketing_copy",
            timeout=settings.agent_timeout_marketing_copy,
        )
        self.llm: ChatOpenAI | None = None
        if settings.llm_api_key:
            self.llm = ChatOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                temperature=0.9,
                max_tokens=2048,
            )

    async def _execute(self, **kwargs: Any) -> MarketingCopyResult:
        user_profile: UserProfile | None = kwargs.get("user_profile")
        products: list[Product] = kwargs.get("products", [])
        context: dict[str, Any] = kwargs.get("context", {})

        if not products:
            return MarketingCopyResult(success=True, copies=[], confidence=1.0)

        template_key = self._select_template(user_profile)
        system_prompt = PROMPT_TEMPLATES[template_key] + self._tone_instruction(context)

        product_info = "\n".join(
            f"- ID:{p.product_id} 名称:{p.name} 类目:{p.category} 价格:¥{p.price} 标签:{','.join(p.tags)}"
            for p in products
        )

        messages = [
            SystemMessage(content=system_prompt + COPY_OUTPUT_INSTRUCTION),
            HumanMessage(content=f"商品列表:\n{product_info}"),
        ]
        if self.llm is None:
            return MarketingCopyResult(
                success=True,
                copies=self._rule_based_copies(products),
                prompt_template_used=f"{template_key.value}_fallback",
                data={
                    "fallback": True,
                    "fallback_reason": "llm_api_key_not_configured",
                    "llm_called": False,
                    "llm_parse_ok": False,
                    "retry_used": False,
                    "retry_attempts": 0,
                    "retry_succeeded": False,
                },
                confidence=0.55,
            )

        raw_outputs: list[tuple[str, str]] = []
        retry_used = False
        retry_attempts = 0
        allowed_ids = [p.product_id for p in products]
        try:
            try:
                response = await self.llm.ainvoke(messages)
                raw_response = str(response.content)
                raw_outputs.append(("initial", raw_response))
                copies = self._valid_copies(self._parse_copies(raw_response), allowed_ids)
            except Exception as exc:
                copies = []
                raw_outputs.append(("initial_error", str(exc)))
            if len(copies) < len(allowed_ids):
                retry_used = True
                for attempt in range(1, COPY_REPAIR_MAX_ATTEMPTS + 1):
                    retry_attempts = attempt
                    repair_messages = self._build_copy_repair_messages(
                        attempt=attempt,
                        products=products,
                        template_key=template_key,
                        system_prompt=system_prompt,
                        previous_output=raw_outputs[-1][1],
                    )
                    try:
                        repair_response = await self.llm.ainvoke(repair_messages)
                        repair_raw = str(repair_response.content)
                    except Exception as exc:
                        repair_raw = f"request_error: {exc}"
                    raw_outputs.append((f"retry_{attempt}", repair_raw))
                    copies = self._valid_copies(self._parse_copies(repair_raw), allowed_ids)
                    if len(copies) >= len(allowed_ids):
                        break
        except Exception as exc:
            copies = self._rule_based_copies(products)
            return MarketingCopyResult(
                success=True,
                copies=copies,
                prompt_template_used=f"{template_key.value}_fallback",
                data={
                    "fallback": True,
                    "fallback_reason": str(exc),
                    "llm_called": True,
                    "llm_parse_ok": False,
                    "raw_response": self._format_raw_copy_outputs(raw_outputs),
                    "retry_used": retry_used,
                    "retry_attempts": retry_attempts,
                    "retry_succeeded": False,
                },
                confidence=0.55,
                error=str(exc),
            )

        fallback_reason = ""
        if len(copies) < len(allowed_ids):
            copies = self._rule_based_copies(products)
            fallback_reason = "llm_output_not_parseable_json"
        copies = [self._compliance_check(c) for c in copies]

        return MarketingCopyResult(
            success=True,
            copies=copies,
            prompt_template_used=template_key.value,
            data={
                "raw_response": self._format_raw_copy_outputs(raw_outputs),
                "fallback": bool(fallback_reason),
                "fallback_reason": fallback_reason,
                "llm_called": True,
                "llm_parse_ok": not bool(fallback_reason),
                "retry_used": retry_used,
                "retry_attempts": retry_attempts,
                "retry_succeeded": retry_used and not bool(fallback_reason),
            },
            confidence=0.9,
        )

    def _tone_instruction(self, context: dict[str, Any]) -> str:
        # 优先使用请求级语气设置；没有设置时再读取执行计划里的 copy_tone。
        tone = str(context.get("copy_tone", "")).strip().lower()
        if not tone:
            plan = context.get("execution_plan", {})
            if isinstance(plan, dict):
                tone = str(plan.get("copy_tone", "default")).strip().lower()
            else:
                tone = "default"
        if tone == "rational":
            return "\nUse concise and factual tone. Avoid hype."
        if tone == "promotion":
            return "\nUse promotion-oriented tone with value highlights."
        if tone == "new_release":
            return "\nEmphasize new arrivals and freshness."
        if tone == "reassure":
            return "\nUse reassuring and trust-building language."
        return ""

    def _select_template(self, profile: UserProfile | None) -> UserSegment:
        if not profile or not profile.segments:
            return UserSegment.ACTIVE
        priority = [
            UserSegment.NEW_USER,
            UserSegment.HIGH_VALUE,
            UserSegment.CHURN_RISK,
            UserSegment.PRICE_SENSITIVE,
            UserSegment.ACTIVE,
        ]
        for seg in priority:
            if seg in profile.segments:
                return seg
        return UserSegment.ACTIVE

    def _parse_copies(self, raw: str) -> list[dict[str, str]]:
        cleaned = raw.strip()
        if not cleaned:
            return []

        # 如果模型把 JSON 包在 markdown 代码块里，优先提取代码块内容。
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()

        # 尽力从混合文本中提取第一个 JSON 数组。
        if not cleaned.startswith("["):
            arr = re.search(r"\[[\s\S]*\]", cleaned)
            if arr:
                cleaned = arr.group(0)

        try:
            parsed = json.loads(cleaned)
            if not isinstance(parsed, list):
                return []
            out: list[dict[str, str]] = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                pid = item.get("product_id")
                copy = item.get("copy")
                if pid is None or copy is None:
                    continue
                out.append({"product_id": str(pid), "copy": str(copy)})
            return out
        except json.JSONDecodeError:
            return []

    def _build_copy_repair_messages(
        self,
        attempt: int,
        products: list[Product],
        template_key: UserSegment,
        system_prompt: str,
        previous_output: str,
    ) -> list[Any]:
        product_rows = [
            {
                "product_id": p.product_id,
                "name": p.name,
                "category": p.category,
                "price": p.price,
                "brand": p.brand,
                "tags": p.tags,
            }
            for p in products
        ]
        return [
            SystemMessage(
                content=(
                    "You are a strict JSON formatter for marketing copy.\n"
                    "Do not explain. Do not use markdown. Return only a JSON array."
                )
            ),
            HumanMessage(
                content=(
                    f"Repair attempt: {attempt}/{COPY_REPAIR_MAX_ATTEMPTS}\n"
                    f"Template segment: {template_key.value}\n"
                    f"Style instruction: {system_prompt[:600]}\n"
                    f"Products: {json.dumps(product_rows, ensure_ascii=False, default=str)}\n"
                    f"Previous invalid output: {previous_output[:1200]}\n"
                    "Return exactly one item for each product in this format:\n"
                    '[{"product_id":"P001","copy":"..."}]\n'
                    "Rules:\n"
                    "1. Use only product_id values from Products.\n"
                    "2. Include every product exactly once.\n"
                    "3. copy must be a non-empty string.\n"
                    "4. Output JSON array only."
                )
            ),
        ]

    @staticmethod
    def _valid_copies(
        copies: list[dict[str, str]], allowed_ids: list[str]
    ) -> list[dict[str, str]]:
        allowed = set(allowed_ids)
        seen: set[str] = set()
        valid: list[dict[str, str]] = []
        for item in copies:
            pid = str(item.get("product_id", "")).strip()
            copy = str(item.get("copy", "")).strip()
            if pid not in allowed or pid in seen or not copy:
                continue
            seen.add(pid)
            valid.append({"product_id": pid, "copy": copy})
        return valid

    @staticmethod
    def _format_raw_copy_outputs(outputs: list[tuple[str, str]]) -> str:
        return "\n\n".join(f"---{label}---\n{raw}" for label, raw in outputs)

    def _compliance_check(self, copy_item: dict[str, str]) -> dict[str, str]:
        """按照广告合规要求过滤禁用词。"""
        text = copy_item.get("copy", "")
        for word in FORBIDDEN_WORDS:
            text = re.sub(re.escape(word), "***", text)
        copy_item["copy"] = text
        return copy_item

    def _rule_based_copies(self, products: list[Product]) -> list[dict[str, str]]:
        """LLM 不可用时，使用确定性规则生成兜底文案。"""
        copies: list[dict[str, str]] = []
        for p in products:
            tag_hint = f" ({p.tags[0]})" if p.tags else ""
            copy = f"{p.name}{tag_hint} | {p.brand} | price {p.price:.0f}. Fits your recent interests."
            copies.append({"product_id": p.product_id, "copy": copy})
        return copies
