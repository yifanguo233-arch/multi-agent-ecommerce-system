from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine, make_url

from models.schemas import Product


metadata = MetaData()

products_table = Table(
    "products",
    metadata,
    Column("product_id", String(32), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("category", String(80), nullable=False, index=True),
    Column("price", Float, nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("brand", String(120), nullable=False, default=""),
    Column("seller_id", String(64), nullable=False, default=""),
    Column("stock", Integer, nullable=False, default=0, index=True),
    Column("tags", Text, nullable=False, default="[]"),
    Column("score", Float, nullable=False, default=0.0, index=True),
    Column("image_url", Text, nullable=False, default=""),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
    Column("updated_at", DateTime, nullable=False, default=datetime.utcnow),
)


BASE_SEED_PRODUCTS: list[Product] = [
    Product(product_id="P001", name="iPhone 16 Pro", category="手机", price=7999, brand="Apple", seller_id="S01", stock=500, tags=["旗舰", "新品", "mobile"], score=98),
    Product(product_id="P002", name="华为 Mate 70", category="手机", price=5999, brand="华为", seller_id="S02", stock=300, tags=["旗舰", "国产", "mobile"], score=96),
    Product(product_id="P003", name="AirPods Pro 3", category="耳机", price=1899, brand="Apple", seller_id="S01", stock=1000, tags=["降噪", "无线", "hot"], score=94),
    Product(product_id="P004", name="Sony WH-1000XM6", category="耳机", price=2499, brand="Sony", seller_id="S03", stock=200, tags=["头戴", "降噪"], score=92),
    Product(product_id="P005", name="iPad Air M3", category="平板", price=4799, brand="Apple", seller_id="S01", stock=400, tags=["学习", "办公"], score=91),
    Product(product_id="P006", name="小米平板7 Pro", category="平板", price=2499, brand="小米", seller_id="S04", stock=600, tags=["性价比", "娱乐"], score=89),
    Product(product_id="P007", name="Anker 140W充电器", category="配件", price=399, brand="Anker", seller_id="S05", stock=2000, tags=["快充", "便携"], score=88),
    Product(product_id="P008", name="机械革命极光X", category="笔记本", price=6999, brand="机械革命", seller_id="S06", stock=150, tags=["游戏", "高性能", "gaming"], score=90),
    Product(product_id="P009", name="戴尔U2724D显示器", category="显示器", price=3299, brand="Dell", seller_id="S07", stock=80, tags=["4K", "办公"], score=86),
    Product(product_id="P010", name="罗技MX Master 3S", category="配件", price=749, brand="罗技", seller_id="S08", stock=500, tags=["无线", "办公"], score=87),
    Product(product_id="P011", name="三星980 Pro 2TB", category="存储", price=1199, brand="三星", seller_id="S09", stock=300, tags=["SSD", "高速"], score=85),
    Product(product_id="P012", name="绿联65W氮化镓", category="配件", price=129, brand="绿联", seller_id="S10", stock=5000, tags=["快充", "性价比"], score=84),
    Product(product_id="P013", name="Apple Watch Ultra 3", category="穿戴", price=5999, brand="Apple", seller_id="S01", stock=200, tags=["运动", "健康"], score=88),
    Product(product_id="P014", name="大疆Mini 4 Pro", category="无人机", price=4788, brand="大疆", seller_id="S11", stock=100, tags=["航拍", "便携"], score=87),
    Product(product_id="P015", name="Switch 2", category="游戏机", price=2499, brand="Nintendo", seller_id="S12", stock=50, tags=["主机", "游戏", "gaming", "hot"], score=93),
]


CATEGORY_BLUEPRINTS: dict[str, dict[str, Any]] = {
    "手机": {"brands": ["Apple", "华为", "小米", "OPPO", "vivo", "荣耀"], "base_price": 2499, "tags": ["旗舰", "拍照", "mobile"]},
    "耳机": {"brands": ["Apple", "Sony", "Bose", "华为", "漫步者"], "base_price": 499, "tags": ["降噪", "无线", "通勤"]},
    "平板": {"brands": ["Apple", "小米", "华为", "三星", "联想"], "base_price": 1999, "tags": ["学习", "办公", "娱乐"]},
    "配件": {"brands": ["Anker", "绿联", "罗技", "倍思", "Belkin"], "base_price": 99, "tags": ["快充", "便携", "性价比"]},
    "笔记本": {"brands": ["联想", "Dell", "华硕", "机械革命", "Apple"], "base_price": 4999, "tags": ["办公", "高性能", "轻薄"]},
    "显示器": {"brands": ["Dell", "AOC", "LG", "三星", "BenQ"], "base_price": 1299, "tags": ["4K", "办公", "护眼"]},
    "存储": {"brands": ["三星", "西部数据", "致态", "闪迪", "铠侠"], "base_price": 299, "tags": ["SSD", "高速", "扩容"]},
    "穿戴": {"brands": ["Apple", "华为", "佳明", "小米", "Amazfit"], "base_price": 699, "tags": ["运动", "健康", "续航"]},
    "无人机": {"brands": ["大疆", "影石", "道通"], "base_price": 2999, "tags": ["航拍", "便携", "创作"]},
    "游戏机": {"brands": ["Nintendo", "Sony", "微软", "ROG", "Steam"], "base_price": 1999, "tags": ["主机", "游戏", "gaming"]},
    "智能家居": {"brands": ["米家", "华为智选", "Aqara", "海尔", "美的"], "base_price": 199, "tags": ["智能", "家用", "自动化"]},
    "摄影": {"brands": ["Canon", "Sony", "Nikon", "富士", "大疆"], "base_price": 3999, "tags": ["影像", "创作", "便携"]},
}


def generate_seed_products(count: int = 240) -> list[Product]:
    """生成确定性的 demo 商品目录，为数据库召回提供足够商品深度。"""
    target = max(100, min(500, int(count)))
    products = [p.model_copy(deep=True) for p in BASE_SEED_PRODUCTS[:target]]
    if len(products) >= target:
        return products[:target]

    categories = list(CATEGORY_BLUEPRINTS.keys())
    variants = ["Air", "Pro", "Max", "Lite", "Ultra", "Neo", "SE", "Plus"]
    tag_pool = ["新品", "热门", "爆款", "促销", "礼物", "学生", "商务", "户外"]

    for i in range(len(products) + 1, target + 1):
        category = categories[(i - 1) % len(categories)]
        blueprint = CATEGORY_BLUEPRINTS[category]
        brands = blueprint["brands"]
        brand = brands[(i * 7) % len(brands)]
        variant = variants[(i * 5) % len(variants)]
        generation = 1 + (i % 9)
        product_id = f"P{i:03d}"
        price = float(blueprint["base_price"] + ((i * 137) % 1800) + (generation * 60))
        if category in {"手机", "笔记本", "摄影", "无人机"}:
            price += 1200
        if category in {"配件", "存储"}:
            price = max(79.0, price * 0.35)
        stock = (i * 37) % 1200
        if i % 23 == 0:
            stock = 0
        elif i % 17 == 0:
            stock = 42
        elif i % 11 == 0:
            stock = 88
        tags = list(dict.fromkeys(blueprint["tags"] + [tag_pool[i % len(tag_pool)]]))
        score = float(55 + ((i * 13) % 45))
        seller_id = f"S{1 + (i % 30):02d}"
        products.append(
            Product(
                product_id=product_id,
                name=f"{brand} {category}{variant} {generation}",
                category=category,
                price=round(price, 2),
                description=f"{brand} {category} demo catalog item, seeded from database for recommendation tracing.",
                brand=brand,
                seller_id=seller_id,
                stock=stock,
                tags=tags,
                score=score,
                image_url=f"https://example.com/products/{product_id}.jpg",
            )
        )
    return products


class ProductRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = self._create_engine(database_url)
        metadata.create_all(self.engine)

    @property
    def safe_database_url(self) -> str:
        try:
            return make_url(self.database_url).render_as_string(hide_password=True)
        except Exception:
            return self.database_url

    def seed_if_empty(self, count: int = 240) -> int:
        if self.count_products() > 0:
            return 0
        products = generate_seed_products(count)
        now = datetime.utcnow()
        rows = [self._product_to_row(p, now=now) for p in products]
        with self.engine.begin() as conn:
            conn.execute(products_table.insert(), rows)
        return len(rows)

    def count_products(self) -> int:
        with self.engine.connect() as conn:
            value = conn.execute(select(func.count()).select_from(products_table)).scalar_one()
        return int(value or 0)

    def list_products(
        self,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        in_stock_only: bool = False,
    ) -> list[Product]:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        stmt = select(products_table)
        if category:
            stmt = stmt.where(products_table.c.category == category)
        if in_stock_only:
            stmt = stmt.where(products_table.c.stock > 0)
        stmt = stmt.order_by(
            products_table.c.score.desc(),
            products_table.c.stock.desc(),
            products_table.c.product_id.asc(),
        ).limit(limit).offset(offset)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_product(row) for row in rows]

    def get_product(self, product_id: str) -> Product | None:
        stmt = select(products_table).where(products_table.c.product_id == product_id)
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_product(row) if row else None

    def get_products_by_ids(self, product_ids: list[str]) -> list[Product]:
        if not product_ids:
            return []
        stmt = select(products_table).where(products_table.c.product_id.in_(product_ids))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        by_id = {str(row["product_id"]): self._row_to_product(row) for row in rows}
        return [by_id[pid] for pid in product_ids if pid in by_id]

    def get_stock(self, product_id: str) -> int | None:
        stmt = select(products_table.c.stock).where(products_table.c.product_id == product_id)
        with self.engine.connect() as conn:
            value = conn.execute(stmt).scalar_one_or_none()
        return int(value) if value is not None else None

    def get_stock_map(self, product_ids: list[str]) -> dict[str, int]:
        if not product_ids:
            return {}
        stmt = select(products_table.c.product_id, products_table.c.stock).where(
            products_table.c.product_id.in_(product_ids)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {str(pid): int(stock) for pid, stock in rows}

    def categories(self) -> list[str]:
        stmt = select(products_table.c.category).distinct().order_by(products_table.c.category.asc())
        with self.engine.connect() as conn:
            return [str(row[0]) for row in conn.execute(stmt).all()]

    def _create_engine(self, database_url: str) -> Engine:
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if database_url.startswith("sqlite:///"):
                raw_path = database_url.replace("sqlite:///", "", 1)
                if raw_path and raw_path not in {":memory:", ""}:
                    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(database_url, **kwargs)

    def _product_to_row(self, product: Product, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.utcnow()
        return {
            "product_id": product.product_id,
            "name": product.name,
            "category": product.category,
            "price": float(product.price),
            "description": product.description,
            "brand": product.brand,
            "seller_id": product.seller_id,
            "stock": int(product.stock),
            "tags": json.dumps(product.tags, ensure_ascii=False),
            "score": float(product.score),
            "image_url": product.image_url,
            "created_at": current,
            "updated_at": current,
        }

    def _row_to_product(self, row: Any) -> Product:
        data = dict(row)
        raw_tags = data.get("tags", "[]")
        try:
            tags = json.loads(raw_tags) if isinstance(raw_tags, str) else list(raw_tags or [])
        except Exception:
            tags = []
        return Product(
            product_id=str(data.get("product_id", "")),
            name=str(data.get("name", "")),
            category=str(data.get("category", "")),
            price=float(data.get("price", 0) or 0),
            description=str(data.get("description", "") or ""),
            brand=str(data.get("brand", "") or ""),
            seller_id=str(data.get("seller_id", "") or ""),
            stock=int(data.get("stock", 0) or 0),
            tags=[str(tag) for tag in tags],
            score=float(data.get("score", 0) or 0),
            image_url=str(data.get("image_url", "") or ""),
        )


@lru_cache(maxsize=8)
def get_product_repository(database_url: str) -> ProductRepository:
    return ProductRepository(database_url)
