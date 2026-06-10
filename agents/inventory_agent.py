"""
InventoryAgent 结构图

输入：
  products + context.execution_plan.risk_policy
      |
      v
批量查询库存
  ProductRepository.get_stock_map(product_ids)
      |
      v
库存处理
  +-- stock <= 0：过滤，不进入可推荐列表
  +-- stock <= 50：critical 低库存提醒
  +-- stock <= 100：warning 低库存提醒
  +-- 热门商品 + 低库存：生成限购策略
  +-- risk_policy=retention_boost 且库存充足：放宽限购
      |
      v
InventoryResult
  available_products / low_stock_alerts / purchase_limits

输出去向：
  available_products -> Graph filter 节点过滤最终商品
  low_stock_alerts / purchase_limits -> 响应调试信息或运营侧观察

LLM 与兜底：
  不调用 LLM，完全确定性执行。
  数据库查不到库存时，回退使用 Product 对象里的 stock 字段。

核心分支：
  库存阈值、热门标签、risk_policy 共同决定过滤和限购。

当前边界：
  stock_guard 目前没有在这里形成独立分支；主要实际消费 retention_boost。
"""

from __future__ import annotations

from typing import Any

from models.schemas import InventoryResult, Product
from services.product_repository import ProductRepository, get_product_repository

from .base_agent import BaseAgent

SAFETY_STOCK_THRESHOLD = 50
LOW_STOCK_THRESHOLD = 100
HOT_ITEM_PURCHASE_LIMIT = 2


class InventoryAgent(BaseAgent):
    def __init__(self):
        from config import get_settings

        settings = get_settings()
        super().__init__(
            name="inventory",
            timeout=settings.agent_timeout_inventory,
        )
        self.product_repository: ProductRepository = get_product_repository(settings.database_url)
        if settings.product_auto_seed:
            self.product_repository.seed_if_empty(settings.product_seed_count)

    async def _execute(self, **kwargs: Any) -> InventoryResult:
        products: list[Product] = kwargs.get("products", [])
        context: dict[str, Any] = kwargs.get("context", {})
        risk_policy = self._extract_risk_policy(context)

        available = []
        low_stock_alerts = []
        purchase_limits: dict[str, int] = {}
        stock_map = await self._stock_map([p.product_id for p in products])
        missing_product_ids: list[str] = []

        for product in products:
            stock = stock_map.get(product.product_id)
            if stock is None:
                missing_product_ids.append(product.product_id)
                stock = product.stock

            if stock <= 0:
                continue

            available.append(product.product_id)

            if stock <= SAFETY_STOCK_THRESHOLD:
                low_stock_alerts.append(
                    {
                        "product_id": product.product_id,
                        "name": product.name,
                        "current_stock": stock,
                        "level": "critical",
                        "action": "urgent_restock",
                    }
                )
            elif stock <= LOW_STOCK_THRESHOLD:
                low_stock_alerts.append(
                    {
                        "product_id": product.product_id,
                        "name": product.name,
                        "current_stock": stock,
                        "level": "warning",
                        "action": "plan_restock",
                    }
                )

            limit = self._calc_purchase_limit(product, stock, risk_policy)
            if limit is not None:
                purchase_limits[product.product_id] = limit

        return InventoryResult(
            success=True,
            available_products=available,
            low_stock_alerts=low_stock_alerts,
            purchase_limits=purchase_limits,
            data={
                "stock_source": "database",
                "database_url": self.product_repository.safe_database_url,
                "total_checked": len(products),
                "available_count": len(available),
                "alert_count": len(low_stock_alerts),
                "stock_snapshot": stock_map,
                "missing_product_ids": missing_product_ids,
            },
            confidence=0.95,
        )

    async def _check_stock(self, product_id: str, fallback_stock: int) -> int:
        stock = self.product_repository.get_stock(product_id)
        return fallback_stock if stock is None else stock

    async def _stock_map(self, product_ids: list[str]) -> dict[str, int]:
        return self.product_repository.get_stock_map(product_ids)

    def _calc_purchase_limit(
        self, product: Product, stock: int, risk_policy: str = "standard"
    ) -> int | None:
        """根据库存深度和商品热度动态计算限购数量。"""
        is_hot = any(tag in {"gaming", "mobile", "hot", "热门", "爆款", "游戏"} for tag in product.tags)
        if risk_policy == "retention_boost" and stock > LOW_STOCK_THRESHOLD:
            return None
        if stock <= SAFETY_STOCK_THRESHOLD:
            return 1
        if stock <= LOW_STOCK_THRESHOLD and is_hot:
            return HOT_ITEM_PURCHASE_LIMIT
        if is_hot and stock <= 300:
            return 3
        return None

    def _extract_risk_policy(self, context: dict[str, Any]) -> str:
        plan = context.get("execution_plan", {})
        if not isinstance(plan, dict):
            return "standard"
        return str(plan.get("risk_policy", "standard"))
