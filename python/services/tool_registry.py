from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class ToolSpec:
    name: str
    fn: ToolFn
    description: str = ""


class ToolRegistry:
    """进程内的轻量 tool registry，供 Agent workflow 调用。"""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, fn: ToolFn, description: str = "") -> None:
        self._tools[name] = ToolSpec(name=name, fn=fn, description=description)

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def run(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            return {"ok": False, "error": f"tool_not_found:{name}"}
        try:
            result = self._tools[name].fn(payload)
            return {"ok": True, "name": name, "result": result}
        except Exception as exc:
            return {"ok": False, "name": name, "error": str(exc)}
