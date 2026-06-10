from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import get_settings
from services.product_repository import get_product_repository


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the demo product catalog database.")
    parser.add_argument("--count", type=int, default=None, help="Seed count, clamped to 100-500.")
    args = parser.parse_args()

    settings = get_settings()
    count = args.count if args.count is not None else settings.product_seed_count
    repo = get_product_repository(settings.database_url)
    inserted = repo.seed_if_empty(count)
    print(
        f"database={repo.safe_database_url} inserted={inserted} total_products={repo.count_products()}"
    )


if __name__ == "__main__":
    main()
