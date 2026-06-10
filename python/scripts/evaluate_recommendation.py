from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import statistics
import sys
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@dataclass
class EvalCase:
    user_id: str
    scene: str
    context: dict[str, Any]
    expected_product_ids: list[str]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    case: EvalCase
    product_ids: list[str]
    categories: list[str]
    latency_ms: float
    fallback: bool
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Offline recommendation evaluator. Generates simulated user behavior "
            "or reads JSON/JSONL cases, then reports coverage, diversity, hit_rate@k, "
            "fallback_rate, and avg_latency."
        )
    )
    parser.add_argument(
        "--mode",
        choices=["lightweight", "internal", "api"],
        default="lightweight",
        help=(
            "lightweight=local recall+inventory, internal=full Supervisor pipeline, "
            "api=POST /api/v1/recommend/graph"
        ),
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--input", default="", help="Optional JSON or JSONL eval cases.")
    parser.add_argument("--output", default="", help="Optional path to write JSON metrics.")
    parser.add_argument("--num-users", type=int, default=30)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    parser.add_argument("--verbose", action="store_true", help="Keep agent info logs enabled.")
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Allow internal mode to use configured LLM/RAG instead of offline fallbacks.",
    )
    return parser.parse_args()


def configure_offline_defaults(args: argparse.Namespace) -> None:
    os.environ.setdefault("ECOM_PRODUCT_AUTO_SEED", "true")
    os.environ.setdefault("ECOM_PRODUCT_SEED_COUNT", "240")
    if args.mode != "api" and not args.use_llm:
        os.environ["ECOM_PLANNER_USE_LLM"] = "false"
        os.environ["ECOM_RAG_ENABLED"] = "false"
        os.environ["ECOM_LLM_API_KEY"] = "offline-eval-key"
        os.environ["ECOM_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"


def configure_logging(verbose: bool) -> None:
    if verbose:
        return
    logging.getLogger().setLevel(logging.WARNING)
    warnings.filterwarnings("ignore")
    try:
        import structlog

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    except Exception:
        pass


def load_input_cases(path: str) -> list[EvalCase]:
    if not path:
        return []
    source = Path(path)
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if source.suffix.lower() == ".jsonl":
        raw_items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        if isinstance(payload, dict):
            raw_items = payload.get("cases") or payload.get("users") or []
        else:
            raw_items = payload
    return [_case_from_mapping(item, idx) for idx, item in enumerate(raw_items)]


def _case_from_mapping(item: dict[str, Any], idx: int) -> EvalCase:
    expected = (
        item.get("expected_product_ids")
        or item.get("target_product_ids")
        or item.get("positive_product_ids")
        or []
    )
    if not expected and item.get("holdout_product_id"):
        expected = [item["holdout_product_id"]]
    if isinstance(expected, str):
        expected = [expected]
    return EvalCase(
        user_id=str(item.get("user_id", f"eval_user_{idx:04d}")),
        scene=str(item.get("scene", "homepage")),
        context=dict(item.get("context", {})),
        expected_product_ids=[str(x) for x in expected if x],
        meta=dict(item.get("meta", {})),
    )


async def fetch_api_catalog(api_url: str, timeout: float) -> tuple[list[dict[str, Any]], int]:
    import httpx

    url = api_url.rstrip("/") + "/api/v1/products"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, params={"limit": 500, "in_stock_only": True})
        response.raise_for_status()
        payload = response.json()
    products = list(payload.get("products", []))
    return products, int(payload.get("total_count", len(products)) or len(products))


def load_local_catalog() -> tuple[list[dict[str, Any]], int]:
    from config import get_settings
    from services.product_repository import get_product_repository

    settings = get_settings()
    repo = get_product_repository(settings.database_url)
    if settings.product_auto_seed:
        repo.seed_if_empty(settings.product_seed_count)
    products = [p.model_dump(mode="json") for p in repo.list_products(limit=500, in_stock_only=True)]
    return products, repo.count_products()


def generate_cases(catalog: list[dict[str, Any]], num_users: int, seed: int) -> list[EvalCase]:
    rng = random.Random(seed)
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in catalog:
        category = str(product.get("category", ""))
        if category:
            by_category[category].append(product)

    categories = sorted(by_category)
    if not categories:
        raise RuntimeError("Cannot generate eval cases: product catalog is empty.")

    cases: list[EvalCase] = []
    for idx in range(max(1, num_users)):
        category = categories[idx % len(categories)]
        pool = sorted(by_category[category], key=lambda p: _product_sequence(str(p.get("product_id", ""))))
        target = pool[(idx // len(categories)) % min(len(pool), 5)]
        brand = str(target.get("brand", ""))
        tags = [str(x) for x in target.get("tags", [])[:2]]
        price = float(target.get("price", 0) or 0)
        context = {
            "session_id": f"eval_session_{idx:04d}",
            "recent_views": [category, category, brand] + tags,
            "recent_clicks": [category, brand],
            "preferred_categories_30d": [category],
            "preferred_brands_30d": [brand] if brand else [],
            "price_sensitivity": round(rng.uniform(0.25, 0.9), 2),
            "avg_order_amount": max(100.0, min(price, 12000.0)),
            "trigger_event": "offline_eval",
        }
        cases.append(
            EvalCase(
                user_id=f"eval_user_{idx:04d}",
                scene="homepage",
                context=context,
                expected_product_ids=[str(target.get("product_id", ""))],
                meta={
                    "target_category": category,
                    "target_name": target.get("name", ""),
                    "simulated": True,
                },
            )
        )
    return cases


def _product_sequence(product_id: str) -> int:
    digits = "".join(ch for ch in product_id if ch.isdigit())
    return int(digits) if digits else 10**9


async def run_lightweight(cases: list[EvalCase], k: int) -> list[EvalResult]:
    from agents.inventory_agent import InventoryAgent
    from agents.product_rec_agent import ProductRecAgent
    from models.schemas import UserProfile

    rec_agent = ProductRecAgent()
    inventory_agent = InventoryAgent()
    results: list[EvalResult] = []

    for case in cases:
        start = time.perf_counter()
        try:
            preferred = _as_str_list(case.context.get("preferred_categories_30d"))
            profile = UserProfile(
                user_id=case.user_id,
                preferred_categories=preferred,
                price_range=(0.0, 20000.0),
                recent_views=_as_str_list(case.context.get("recent_views")),
            )
            context = {
                **case.context,
                "execution_plan": {"retrieve_strategy": "hybrid", "rerank_focus": "balanced"},
            }
            candidates = await rec_agent._recall(profile=profile, context=context, limit=max(k * 3, k))
            inventory_result = await inventory_agent.run(products=candidates, context=context)
            available = set(getattr(inventory_result, "available_products", []))
            final_products = [p for p in candidates if p.product_id in available][:k]
            latency_ms = (time.perf_counter() - start) * 1000
            results.append(
                EvalResult(
                    case=case,
                    product_ids=[p.product_id for p in final_products],
                    categories=[p.category for p in final_products],
                    latency_ms=latency_ms,
                    fallback=(not inventory_result.success) or len(final_products) < min(k, len(candidates)),
                )
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            results.append(EvalResult(case=case, product_ids=[], categories=[], latency_ms=latency_ms, fallback=True, error=str(exc)))
    return results


class OfflineLLM:
    async def ainvoke(self, messages: Any) -> Any:
        raise RuntimeError("LLM disabled for offline evaluation; pass --use-llm to enable it.")


async def run_internal(cases: list[EvalCase], k: int, use_llm: bool) -> list[EvalResult]:
    from models.schemas import RecommendationRequest
    from orchestrator.supervisor import SupervisorOrchestrator

    supervisor = SupervisorOrchestrator()
    if not use_llm:
        supervisor.user_profile_agent.llm = OfflineLLM()
        supervisor.product_rec_agent.llm = OfflineLLM()
        supervisor.marketing_copy_agent.llm = OfflineLLM()
    results: list[EvalResult] = []
    for case in cases:
        start = time.perf_counter()
        try:
            response = await supervisor.recommend(
                RecommendationRequest(
                    user_id=case.user_id,
                    scene=case.scene,
                    num_items=k,
                    business_goal="offline_eval",
                    context=case.context,
                )
            )
            latency_ms = float(response.total_latency_ms or ((time.perf_counter() - start) * 1000))
            products = list(response.products)
            results.append(
                EvalResult(
                    case=case,
                    product_ids=[p.product_id for p in products[:k]],
                    categories=[p.category for p in products[:k]],
                    latency_ms=latency_ms,
                    fallback=_response_used_fallback(response.agent_results),
                )
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            results.append(EvalResult(case=case, product_ids=[], categories=[], latency_ms=latency_ms, fallback=True, error=str(exc)))
    return results


async def run_api(cases: list[EvalCase], k: int, api_url: str, timeout: float) -> list[EvalResult]:
    import httpx

    endpoint = api_url.rstrip("/") + "/api/v1/recommend/graph"
    results: list[EvalResult] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for case in cases:
            body = {
                "user_id": case.user_id,
                "scene": case.scene,
                "num_items": k,
                "business_goal": "offline_eval",
                "context": case.context,
            }
            start = time.perf_counter()
            try:
                response = await client.post(endpoint, json=body)
                response.raise_for_status()
                payload = response.json()
                latency_ms = (time.perf_counter() - start) * 1000
                products = list(payload.get("products", []))[:k]
                results.append(
                    EvalResult(
                        case=case,
                        product_ids=[str(p.get("product_id", "")) for p in products],
                        categories=[str(p.get("category", "")) for p in products],
                        latency_ms=latency_ms,
                        fallback=_agent_results_used_fallback(payload.get("agent_results", {})),
                    )
                )
            except Exception as exc:
                latency_ms = (time.perf_counter() - start) * 1000
                results.append(EvalResult(case=case, product_ids=[], categories=[], latency_ms=latency_ms, fallback=True, error=str(exc)))
    return results


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value:
        return [value]
    return []


def _response_used_fallback(agent_results: dict[str, Any]) -> bool:
    return _agent_results_used_fallback(
        {
            name: result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            for name, result in agent_results.items()
        }
    )


def _agent_results_used_fallback(agent_results: dict[str, Any]) -> bool:
    for result in (agent_results or {}).values():
        if not isinstance(result, dict):
            continue
        if result.get("success") is False:
            return True
        data = result.get("data", {})
        if isinstance(data, dict) and data.get("fallback") is True:
            return True
        template = str(result.get("prompt_template_used", ""))
        if template.endswith("_fallback"):
            return True
    return False


def compute_metrics(
    cases: list[EvalCase],
    results: list[EvalResult],
    catalog_count: int,
    k: int,
    mode: str,
) -> dict[str, Any]:
    unique_recommended: set[str] = set()
    per_request_diversity: list[float] = []
    hits = 0
    fallback_count = 0
    errors = 0
    latencies: list[float] = []

    samples: list[dict[str, Any]] = []
    for case, result in zip(cases, results):
        rec_ids = [pid for pid in result.product_ids[:k] if pid]
        unique_recommended.update(rec_ids)
        if rec_ids:
            per_request_diversity.append(len({c for c in result.categories[: len(rec_ids)] if c}) / len(rec_ids))
        else:
            per_request_diversity.append(0.0)
        expected = set(case.expected_product_ids)
        if expected and expected.intersection(rec_ids):
            hits += 1
        if result.fallback:
            fallback_count += 1
        if result.error:
            errors += 1
        latencies.append(result.latency_ms)
        if len(samples) < 5:
            samples.append(
                {
                    "user_id": case.user_id,
                    "expected_product_ids": case.expected_product_ids,
                    "recommended_product_ids": rec_ids,
                    "hit": bool(expected.intersection(rec_ids)) if expected else False,
                    "latency_ms": round(result.latency_ms, 2),
                    "fallback": result.fallback,
                    "error": result.error,
                }
            )

    total = max(1, len(results))
    evaluated_hits = sum(1 for c in cases if c.expected_product_ids)
    hit_denominator = max(1, evaluated_hits)
    return {
        "mode": mode,
        "k": k,
        "num_cases": len(results),
        "catalog_count": catalog_count,
        "coverage": round(len(unique_recommended) / max(1, catalog_count), 6),
        "recommended_unique_products": len(unique_recommended),
        "diversity": round(statistics.mean(per_request_diversity), 6) if per_request_diversity else 0.0,
        f"hit_rate@{k}": round(hits / hit_denominator, 6),
        "hits": hits,
        "hit_eval_cases": evaluated_hits,
        "fallback_rate": round(fallback_count / total, 6),
        "fallback_count": fallback_count,
        "avg_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p95_latency_ms": round(_percentile(latencies, 0.95), 2) if latencies else 0.0,
        "error_rate": round(errors / total, 6),
        "error_count": errors,
        "samples": samples,
    }


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


def print_report(metrics: dict[str, Any]) -> None:
    k = metrics["k"]
    print("Offline Recommendation Evaluation")
    print("=" * 40)
    print(f"mode: {metrics['mode']}")
    print(f"cases: {metrics['num_cases']}")
    print(f"k: {k}")
    print(f"catalog_count: {metrics['catalog_count']}")
    print(f"coverage: {metrics['coverage']:.4f} ({metrics['recommended_unique_products']} unique products)")
    print(f"diversity: {metrics['diversity']:.4f}")
    print(f"hit_rate@{k}: {metrics[f'hit_rate@{k}']:.4f} ({metrics['hits']}/{metrics['hit_eval_cases']})")
    print(f"fallback_rate: {metrics['fallback_rate']:.4f} ({metrics['fallback_count']}/{metrics['num_cases']})")
    print(f"avg_latency_ms: {metrics['avg_latency_ms']:.2f}")
    print(f"p95_latency_ms: {metrics['p95_latency_ms']:.2f}")
    print(f"error_rate: {metrics['error_rate']:.4f} ({metrics['error_count']}/{metrics['num_cases']})")
    print("\nSample cases")
    for sample in metrics["samples"]:
        print(
            "- user={user_id} hit={hit} expected={expected_product_ids} "
            "recommended={recommended_product_ids} latency_ms={latency_ms} fallback={fallback}".format(**sample)
        )
        if sample.get("error"):
            print(f"  error={sample['error']}")


async def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_input_cases(args.input)
    catalog: list[dict[str, Any]]
    catalog_count: int
    if args.mode == "api" and not cases:
        catalog, catalog_count = await fetch_api_catalog(args.api_url, args.timeout)
    else:
        catalog, catalog_count = load_local_catalog()

    if not cases:
        cases = generate_cases(catalog=catalog, num_users=args.num_users, seed=args.seed)

    if args.mode == "lightweight":
        results = await run_lightweight(cases, args.k)
    elif args.mode == "internal":
        results = await run_internal(cases, args.k, args.use_llm)
    else:
        results = await run_api(cases, args.k, args.api_url, args.timeout)

    return compute_metrics(
        cases=cases,
        results=results,
        catalog_count=catalog_count,
        k=args.k,
        mode=args.mode,
    )


def main() -> None:
    args = parse_args()
    configure_offline_defaults(args)
    configure_logging(args.verbose)
    metrics = asyncio.run(evaluate(args))
    if args.output:
        Path(args.output).write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print_report(metrics)


if __name__ == "__main__":
    main()
