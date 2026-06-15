import asyncio

import main
from models.schemas import Product


def test_get_product_response_excludes_links(monkeypatch):
    class StubRepo:
        safe_database_url = "sqlite:///test.db"

        def get_product(self, product_id: str):
            return Product(
                product_id=product_id,
                name="Test Product",
                category="demo",
                price=99.0,
                stock=12,
            )

    monkeypatch.setattr(main, "product_repository", StubRepo())

    result = asyncio.run(main.get_product("P130"))

    assert result["source"] == "database"
    assert result["database_url"] == "sqlite:///test.db"
    assert result["product"]["product_id"] == "P130"
    assert "_links" not in result


def test_get_product_inventory_response_excludes_links(monkeypatch):
    class StubRepo:
        safe_database_url = "sqlite:///test.db"

        def get_product(self, product_id: str):
            return Product(
                product_id=product_id,
                name="Test Product",
                category="demo",
                price=99.0,
                stock=12,
            )

    monkeypatch.setattr(main, "product_repository", StubRepo())

    result = asyncio.run(main.get_product_inventory("P123"))

    assert result["source"] == "database"
    assert result["database_url"] == "sqlite:///test.db"
    assert result["product_id"] == "P123"
    assert result["stock"] == 12
    assert result["available"] is True
    assert "_links" not in result


def test_openapi_schema_excludes_response_links():
    schema = main.app.openapi()

    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            for response in operation.get("responses", {}).values():
                assert "links" not in response
