import asyncio

from agents.inventory_agent import InventoryAgent
from models.schemas import Product
from services.product_repository import ProductRepository


def test_product_repository_seeds_and_reads_catalog():
    repo = ProductRepository("sqlite:///:memory:")

    inserted = repo.seed_if_empty(120)

    assert inserted == 120
    assert repo.count_products() == 120
    product = repo.get_product("P015")
    assert product is not None
    assert product.name == "Switch 2"
    assert repo.get_stock("P015") == 50


def test_inventory_agent_uses_database_stock():
    repo = ProductRepository("sqlite:///:memory:")
    repo.seed_if_empty(120)
    agent = InventoryAgent()
    agent.product_repository = repo

    stale_product = Product(
        product_id="P015",
        name="Switch 2",
        category="游戏机",
        price=2499,
        stock=9999,
        tags=["gaming"],
    )

    result = asyncio.run(agent.run(products=[stale_product], context={}))

    assert result.data["stock_source"] == "database"
    assert result.data["stock_snapshot"]["P015"] == 50
    assert result.purchase_limits["P015"] == 1
