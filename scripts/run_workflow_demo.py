from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

# 允许从仓库根目录运行：`python scripts/run_workflow_demo.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from orchestrator.graph import recommendation_graph_context


KEY_FIELDS = [
    "plan_version",
    "plan_error",
    "selected_tools",
    "reflection_count",
    "final_products",
    "marketing_copies",
    "errors",
]


def _shorten_value(value: Any, limit: int = 220) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def _extract_changed_fields(update_payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in KEY_FIELDS:
        if key in update_payload:
            out[key] = update_payload[key]
    return out


def _summarize_value(value: Any) -> Any:
    if isinstance(value, list):
        if not value:
            return {"type": "list", "len": 0}
        first = value[0]
        if isinstance(first, dict):
            return {"type": "list[dict]", "len": len(value), "keys": sorted(first.keys())[:8]}
        return {"type": f"list[{type(first).__name__}]", "len": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "len": len(value), "keys": sorted(value.keys())[:12]}
    if isinstance(value, set):
        return {"type": "set", "len": len(value)}
    return value


def _diff_payload(prev: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for k, v in payload.items():
        before = prev.get(k, "<missing>")
        after = v
        if before != after:
            diff[k] = {
                "before": _summarize_value(before),
                "after": _summarize_value(after),
            }
    return diff


async def run_demo(
    user_id: str,
    scene: str,
    num_items: int,
    business_goal: str,
    force_reflection: bool,
    max_reflections: int,
) -> None:
    thread_id = str(uuid.uuid4())
    state = {
        "request_id": thread_id,
        "user_id": user_id,
        "scene": scene,
        "num_items": num_items,
        "business_goal": business_goal,
        "max_reflections": max_reflections,
        "context": {"force_reflection": force_reflection},
    }
    cfg = {"configurable": {"thread_id": thread_id}}

    print("=" * 80)
    print("Workflow Demo Start")
    print(f"thread_id={thread_id}")
    print(f"input={_shorten_value(state)}")
    print("=" * 80)

    async with recommendation_graph_context() as graph:
        step = 0
        current_state: dict[str, Any] = dict(state)
        async for chunk in graph.astream(state, config=cfg, stream_mode="updates"):
            step += 1
            # updates mode 通常返回：{"node_name": {...delta...}}
            if isinstance(chunk, dict):
                for node, payload in chunk.items():
                    print(f"\n[step {step}] node={node}")
                    if isinstance(payload, dict):
                        changed = _extract_changed_fields(payload)
                        field_diff = _diff_payload(current_state, payload)
                        print("delta_keys:", sorted(payload.keys()))
                        if changed:
                            print("key_changes:", _shorten_value(changed))
                        print("field_diff:", _shorten_value(field_diff))
                        current_state.update(payload)
                    else:
                        print("delta:", _shorten_value(payload))
            else:
                print(f"\n[step {step}] chunk={_shorten_value(chunk)}")

        final = await graph.aget_state(cfg)
    values = getattr(final, "values", {}) or {}
    print("\n" + "=" * 80)
    print("Workflow Final State (summary)")
    print(f"request_id={values.get('request_id')}")
    print(f"plan_version={values.get('plan_version')}")
    print(f"plan_error={values.get('plan_error')}")
    print(f"reflection_count={values.get('reflection_count')}")
    print(f"selected_tools={values.get('selected_tools')}")
    print(f"tool_outputs_keys={list((values.get('tool_outputs') or {}).keys())}")
    print(f"final_products_count={len(values.get('final_products', []))}")
    print(f"marketing_copies_count={len(values.get('marketing_copies', []))}")
    print(f"trace_steps_count={len(values.get('trace_steps', []))}")
    print(f"errors={_shorten_value(values.get('errors', []))}")
    print("=" * 80)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LangGraph workflow demo with step-by-step state deltas.")
    parser.add_argument("--user-id", default="u001")
    parser.add_argument("--scene", default="homepage")
    parser.add_argument("--num-items", type=int, default=5)
    parser.add_argument("--business-goal", default="conversion")
    parser.add_argument("--force-reflection", action="store_true")
    parser.add_argument("--max-reflections", type=int, default=1)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        run_demo(
            user_id=args.user_id,
            scene=args.scene,
            num_items=args.num_items,
            business_goal=args.business_goal,
            force_reflection=args.force_reflection,
            max_reflections=args.max_reflections,
        )
    )
