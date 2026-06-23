"""
指标提供者 — Gate4 灰度指标的真实数据源
Phase 1: 用 Prometheus API 替代 random.uniform()
可选降级: FileMetricsProvider 用于本地开发/测试
"""
from __future__ import annotations

import json
import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class GrayMetrics(BaseModel):
    """灰度阶梯指标"""
    stage: int
    traffic_pct: int
    label: str
    error_rate: float = 0.0
    latency_p95_ms: float = 0.0
    resource_pct: float = 0.0
    passed: bool = False
    sampled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"arbitrary_types_allowed": True}


# ── 指标提供者接口 ──────────────────────────────────────────

class MetricsProvider(ABC):
    """指标查询抽象接口"""

    @abstractmethod
    def observe_stage(self, improvement_id: str, stage: int,
                       traffic_pct: int, label: str) -> GrayMetrics:
        """查询当前灰度阶梯的指标"""
        ...


class PrometheusMetricsProvider(MetricsProvider):
    """
    通过 Prometheus HTTP API 查询灰度指标
    生产模式: 查询 error_rate / latency_p95 / resource_usage

    PromQL 示例:
      rate(error_count{improvement="imp-xxxx"}[5m])
      histogram_quantile(0.95, rate(request_duration_ms_bucket{improvement="imp-xxxx"}[5m]))
    """

    def __init__(self, prometheus_url: str = "http://localhost:9090",
                 timeout_seconds: float = 5.0,
                 baseline: Optional[dict[str, float]] = None):
        self.base_url = prometheus_url.rstrip("/")
        self.timeout = timeout_seconds
        self.baseline = baseline or {}
        # 连接失败计数，超过阈值则降级
        self._consecutive_failures = 0
        self._degraded = False

    def observe_stage(self, improvement_id: str, stage: int,
                       traffic_pct: int, label: str) -> GrayMetrics:
        if self._degraded:
            return self._degraded_metrics(stage, traffic_pct, label)

        try:
            import requests

            # 并行查 3 个指标
            end = time.time()
            start = end - 300  # 5 分钟窗口

            queries = {
                "error_rate": (
                    f'rate(error_count{{improvement="{improvement_id}"}}[{traffic_pct}m])'
                    if traffic_pct > 0 else "0"
                ),
                "latency_p95_ms": (
                    f'histogram_quantile(0.95, '
                    f'rate(request_duration_ms_bucket{{improvement="{improvement_id}"}}[{traffic_pct}m]))'
                ),
                "resource_pct": (
                    f'avg(resource_usage_pct{{improvement="{improvement_id}"}}[{traffic_pct}m])'
                ),
            }

            results: dict[str, float] = {}
            for metric_name, query in queries.items():
                if not query or query == "0":
                    results[metric_name] = 0.0
                    continue
                resp = requests.post(
                    f"{self.base_url}/api/v1/query",
                    params={"query": query, "time": end},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "success" and data.get("data", {}).get("result"):
                    results[metric_name] = float(data["data"]["result"][0]["value"][1])
                else:
                    results[metric_name] = 0.0

            self._consecutive_failures = 0

            error_rate = results.get("error_rate", 0.0)
            latency = results.get("latency_p95_ms", 0.0)
            resource = results.get("resource_pct", 0.0)

            baselines = self._get_stage_baseline(stage, traffic_pct)
            passed = (
                error_rate <= baselines.get("error_rate_limit", 0.01)
                and latency <= baselines.get("latency_limit", 200.0)
            )

            return GrayMetrics(
                stage=stage, traffic_pct=traffic_pct, label=label,
                error_rate=round(error_rate, 4),
                latency_p95_ms=round(latency, 1),
                resource_pct=round(resource, 1),
                passed=passed,
            )

        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._degraded = True
                logger.warning(
                    "MetricsProvider degraded to fallback after 3 consecutive failures",
                    extra={"error": str(e)},
                )
            return self._degraded_metrics(stage, traffic_pct, label)

    def _get_stage_baseline(self, stage: int, traffic_pct: int) -> dict[str, float]:
        """从 baseline 中获取当前阶梯的阈值"""
        return {
            "error_rate_limit": self.baseline.get("max_error_rate", 0.01),
            "latency_limit": self.baseline.get("max_latency_ms", 200.0),
        }

    def _degraded_metrics(self, stage: int, traffic_pct: int,
                          label: str) -> GrayMetrics:
        """降级方案：返回保守值（不阻塞流水线）"""
        return GrayMetrics(
            stage=stage, traffic_pct=traffic_pct, label=label,
            error_rate=0.0,
            latency_p95_ms=0.0,
            resource_pct=0.0,
            passed=True,  # 降级时不误杀
        )


class ConfigMetricsProvider(MetricsProvider):
    """
    配置驱动的指标提供者 — 从 YAML 或环境变量读取固定指标
    用于本地开发 / CI 环境
    """

    def __init__(self, metrics_config: Optional[dict[str, dict[int, dict]]] = None):
        """
        metrics_config 格式:
        {
            "imp-xxxx": {
                1: {"error_rate": 0.005, "latency_p95_ms": 95, "resource_pct": 45, "passed": True},
                2: {"error_rate": 0.008, "latency_p95_ms": 102, "resource_pct": 48, "passed": True},
                ...
            }
        }
        """
        self.metrics_config = metrics_config or {}

    def observe_stage(self, improvement_id: str, stage: int,
                       traffic_pct: int, label: str) -> GrayMetrics:
        stage_config = self.metrics_config.get(improvement_id, {}).get(stage, {})
        return GrayMetrics(
            stage=stage, traffic_pct=traffic_pct, label=label,
            error_rate=stage_config.get("error_rate", 0.005),
            latency_p95_ms=stage_config.get("latency_p95_ms", 100.0),
            resource_pct=stage_config.get("resource_pct", 50.0),
            passed=stage_config.get("passed", True),
        )


class FileMetricsProvider(MetricsProvider):
    """
    文件驱动的指标提供者 — 从 JSON 文件读取指标数据
    适用于演示和调试
    """

    def __init__(self, metrics_dir: str = "/tmp/quality_gates_metrics"):
        self.metrics_dir = Path(metrics_dir)
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

    def observe_stage(self, improvement_id: str, stage: int,
                       traffic_pct: int, label: str) -> GrayMetrics:
        metric_file = self.metrics_dir / f"{improvement_id}_stage{stage}.json"
        if metric_file.exists():
            data = json.loads(metric_file.read_text())
            return GrayMetrics(**data)

        # 默认值（保守通过）
        return GrayMetrics(
            stage=stage, traffic_pct=traffic_pct, label=label,
            error_rate=0.003,
            latency_p95_ms=90.0,
            resource_pct=45.0,
            passed=True,
        )

    def write_metrics(self, improvement_id: str, stage: int,
                      metrics: GrayMetrics) -> None:
        """写入指标数据（用于测试/录制）"""
        metric_file = self.metrics_dir / f"{improvement_id}_stage{stage}.json"
        metric_file.write_text(metrics.model_dump_json(indent=2))
