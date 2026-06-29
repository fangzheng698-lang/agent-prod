"""Embedded Prometheus-compatible metrics endpoint.

Zero external dependencies. Produces standard Prometheus text format.
Replaces the need for prometheus_client + Pushgateway in single-node mode.

Usage:
    from agent_prod.observability.metrics import MetricsRegistry
    registry = MetricsRegistry()
    registry.counter("http_requests_total").inc()
    registry.histogram("request_duration_ms").observe(234.5)

    @app.get("/metrics")
    async def metrics():
        return Response(registry.render(), media_type="text/plain")

The /metrics endpoint is scraped directly by Prometheus (or viewed by humans).
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock


class Metric:
    """Base metric with labels support."""

    def __init__(self, name: str, help_text: str = "", labels: dict | None = None):
        self.name = name
        self.help = help_text
        self.labels = labels or {}

    def _label_str(self) -> str:
        if not self.labels:
            return ""
        parts = [f'{k}="{v}"' for k, v in sorted(self.labels.items())]
        return "{" + ",".join(parts) + "}"


class Counter(Metric):
    """Monotonic counter (Prometheus counter type)."""

    def __init__(self, name: str, help_text: str = ""):
        super().__init__(name, help_text)
        self._value: float = 0.0

    def inc(self, amount: float = 1.0):
        self._value += amount

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}".strip()]
        lines.append(f"# TYPE {self.name} counter")
        lines.append(f"{self.name} {self._value}")
        return "\n".join(lines)


class Gauge(Metric):
    """Point-in-time value (Prometheus gauge type)."""

    def __init__(self, name: str, help_text: str = ""):
        super().__init__(name, help_text)
        self._value: float = 0.0

    def set(self, value: float):
        self._value = value

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}".strip()]
        lines.append(f"# TYPE {self.name} gauge")
        lines.append(f"{self.name} {self._value}")
        return "\n".join(lines)


class Histogram(Metric):
    """Histogram with configurable buckets (Prometheus histogram type)."""

    def __init__(
        self,
        name: str,
        help_text: str = "",
        buckets: list[float] | None = None,
    ):
        super().__init__(name, help_text)
        self.buckets = buckets or [
            1, 5, 10, 25, 50, 100, 250, 500,
            1000, 2500, 5000, 10000, 30000, 60000, float("inf"),
        ]
        self._sum: float = 0.0
        self._count: int = 0
        self._bucket_counts: dict[float, int] = defaultdict(int)

    def observe(self, value: float):
        self._sum += value
        self._count += 1
        for b in sorted(self.buckets):
            if value <= b:
                self._bucket_counts[b] += 1
                break

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}".strip()]
        lines.append(f"# TYPE {self.name} histogram")
        cumulative = 0
        for b in sorted(self.buckets):
            cumulative += self._bucket_counts[b]
            bs = f"{b}" if b != float("inf") else "+Inf"
            lines.append(f'{self.name}_bucket{{le="{bs}"}} {cumulative}')
        lines.append(f"{self.name}_sum {self._sum}")
        lines.append(f"{self.name}_count {self._count}")
        return "\n".join(lines)


class LabeledCounter(Metric):
    """Counter with labels. Supports per-label increments."""

    def __init__(self, name: str, help_text: str = "", label_names: list[str] | None = None):
        super().__init__(name, help_text)
        self._label_names = label_names or []
        self._values: dict[tuple, float] = defaultdict(float)

    def labels(self, **kwargs) -> LabeledCounter:
        """Return a label-scoped view."""
        _key = tuple(kwargs.get(n, "") for n in self._label_names)
        return self

    def inc(self, amount: float = 1.0, **label_values):
        key = tuple(label_values.get(n, "") for n in self._label_names)
        self._values[key] += amount

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}".strip()]
        lines.append(f"# TYPE {self.name} counter")
        for key, val in self._values.items():
            label_parts = [f'{n}="{v}"' for n, v in zip(self._label_names, key)]
            label_str = "{" + ",".join(label_parts) + "}" if label_parts else ""
            lines.append(f"{self.name}{label_str} {val}")
        if not self._values:
            lines.append(f"{self.name} 0")
        return "\n".join(lines)


class MetricsRegistry:
    """Thread-safe metrics registry.

    Usage:
        registry = MetricsRegistry()
        registry.counter("requests_total", "Total HTTP requests").inc()
        registry.gauge("active_runs", "Active agent runs").set(5)
        registry.histogram("run_duration_ms", "Run duration").observe(1234.5)
    """

    def __init__(self):
        self._lock = Lock()
        self._metrics: dict[str, Metric] = {}
        self._start_time = time.time()

    def counter(self, name: str, help_text: str = "") -> Counter:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Counter(name, help_text)
            return self._metrics[name]  # type: ignore

    def gauge(self, name: str, help_text: str = "") -> Gauge:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Gauge(name, help_text)
            return self._metrics[name]  # type: ignore

    def histogram(self, name: str, help_text: str = "", buckets: list[float] | None = None) -> Histogram:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Histogram(name, help_text, buckets)
            return self._metrics[name]  # type: ignore

    def labeled_counter(self, name: str, help_text: str = "", label_names: list[str] | None = None) -> LabeledCounter:
        """Convenience: register labeled counter if not exists, then return."""
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = LabeledCounter(name, help_text, label_names)
            return self._metrics[name]  # type: ignore

    def get(self, name: str) -> Metric | None:
        return self._metrics.get(name)

    def render(self) -> str:
        """Render all metrics in Prometheus text format."""
        lines = []
        # Process-level metrics
        uptime = time.time() - self._start_time
        lines.append("# HELP agent_prod_uptime_seconds Total uptime")
        lines.append("# TYPE agent_prod_uptime_seconds gauge")
        lines.append(f"agent_prod_uptime_seconds {uptime:.1f}")

        with self._lock:
            for metric in self._metrics.values():
                lines.append(metric.render())
        lines.append("")  # trailing newline required by spec
        lines.append("# EOF")
        return "\n".join(lines)


# ── Global singleton ──
_global_registry: MetricsRegistry | None = None


def get_registry() -> MetricsRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = MetricsRegistry()
    return _global_registry
