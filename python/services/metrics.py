"""
监控指标收集
- Agent调用成功率 / 延迟
- 推荐CTR / CVR / GMV
- A/B测试实验指标
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST


@dataclass
class AgentMetric:
    call_count: int = 0
    success_count: int = 0
    total_latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.call_count if self.call_count else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.call_count if self.call_count else 0.0


class MetricsCollector:
    """内存版 metrics collector；生产环境可替换为 Prometheus 体系。"""

    def __init__(self):
        self._agent_metrics: dict[str, AgentMetric] = defaultdict(AgentMetric)
        self._business_events: list[dict[str, Any]] = []
        self._req_total = Counter(
            "ecom_requests_total",
            "Total recommendation requests",
            ["entrypoint", "mode", "status"],
        )
        self._req_latency = Histogram(
            "ecom_request_latency_ms",
            "Recommendation request latency in milliseconds",
            ["entrypoint", "mode"],
            buckets=(50, 100, 300, 500, 1000, 2000, 5000, 10000, 20000),
        )
        self._agent_total = Counter(
            "ecom_agent_calls_total",
            "Total agent calls",
            ["agent", "status"],
        )
        self._agent_latency = Histogram(
            "ecom_agent_latency_ms",
            "Agent latency in milliseconds",
            ["agent"],
            buckets=(10, 30, 50, 100, 300, 500, 1000, 3000, 10000),
        )

    def record_agent_call(self, agent_name: str, success: bool, latency_ms: float, error: str = ""):
        m = self._agent_metrics[agent_name]
        m.call_count += 1
        if success:
            m.success_count += 1
        m.total_latency_ms += latency_ms
        if error:
            m.errors.append(error)
        status = "success" if success else "failed"
        self._agent_total.labels(agent=agent_name, status=status).inc()
        self._agent_latency.labels(agent=agent_name).observe(max(0.0, latency_ms))

    def record_business_event(self, event_type: str, **kwargs: Any):
        """记录 CTR/CVR/GMV 等业务事件，供 analytics 使用。"""
        self._business_events.append({
            "type": event_type,
            "timestamp": time.time(),
            **kwargs,
        })

    def record_request(
        self,
        entrypoint: str,
        mode: str,
        success: bool,
        latency_ms: float,
    ) -> None:
        status = "success" if success else "failed"
        self._req_total.labels(entrypoint=entrypoint, mode=mode, status=status).inc()
        self._req_latency.labels(entrypoint=entrypoint, mode=mode).observe(max(0.0, latency_ms))

    def get_agent_stats(self) -> dict[str, dict[str, Any]]:
        result = {}
        for name, m in self._agent_metrics.items():
            result[name] = {
                "call_count": m.call_count,
                "success_rate": round(m.success_rate, 4),
                "avg_latency_ms": round(m.avg_latency_ms, 1),
                "recent_errors": m.errors[-5:],
            }
        return result

    def get_business_stats(self) -> dict[str, Any]:
        if not self._business_events:
            return {}
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in self._business_events:
            by_type[e["type"]].append(e)
        stats = {}
        for t, events in by_type.items():
            stats[t] = {"count": len(events)}
        return stats

    def prometheus_payload(self) -> tuple[bytes, str]:
        return generate_latest(), CONTENT_TYPE_LATEST
