"""
Phase 5.3: Performance Profiling — 性能剖析

在 middleware 层记录每个 gate 执行时间，生成性能报告。
支持 start/stop/report 生命周期。

用法:
    profiler = Profiler()
    profiler.start()

    # 在 gate 执行处记录
    profiler.record_gate("gate1_execution", duration_ms=12.5, passed=True)

    # 或使用 context manager
    with gate_profile(profiler, "gate1"):
        do_work()

    profiler.stop()
    print(profiler.report().to_text())

集成到 main.py:
    install_middleware_profiler(app, profiler)
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProfileRecord:
    """单次门执行记录"""

    gate_name: str
    duration_ms: float
    passed: bool | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class GateStats:
    """单道门的统计信息"""

    gate_name: str
    calls: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    passed_count: int = 0
    failed_count: int = 0
    durations: list[float] = field(default_factory=list)

    @property
    def avg_ms(self) -> float:
        if self.calls == 0:
            return 0.0
        return self.total_ms / self.calls

    @property
    def p50_ms(self) -> float:
        return self._percentile(0.50)

    @property
    def p95_ms(self) -> float:
        return self._percentile(0.95)

    @property
    def p99_ms(self) -> float:
        return self._percentile(0.99)

    def _percentile(self, p: float) -> float:
        if not self.durations:
            return 0.0
        sorted_d = sorted(self.durations)
        n = len(sorted_d)
        idx = int(n * p)
        if idx >= n:
            idx = n - 1
        return sorted_d[idx]


@dataclass
class ProfileReport:
    """Profiler 报告"""

    gate_stats: dict[str, GateStats] = field(default_factory=dict)
    total_calls: int = 0
    total_time_ms: float = 0.0
    elapsed_sec: float = 0.0
    _start_time: float = 0.0
    _stop_time: float = 0.0

    def to_text(self) -> str:
        """生成人类可读的文本报告"""
        lines = [
            "=" * 60,
            "  PERFORMANCE PROFILE REPORT",
            "=" * 60,
            f"  Duration:       {self.elapsed_sec:.2f}s",
            f"  Total Calls:    {self.total_calls}",
            f"  Total Time:     {self.total_time_ms:.1f}ms",
            "",
            f"  {'Gate':<30} {'Calls':>6} {'Avg(ms)':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'Pass%':>7}",
            "  " + "-" * 83,
        ]

        for name, stats in sorted(
            self.gate_stats.items(),
            key=lambda x: -x[1].total_ms,
        ):
            pass_pct = (
                stats.passed_count / stats.calls * 100
                if stats.calls > 0
                else 0.0
            )
            lines.append(
                f"  {name:<30} {stats.calls:>6} {stats.avg_ms:>8.1f} "
                f"{stats.p50_ms:>8.1f} {stats.p95_ms:>8.1f} {stats.p99_ms:>8.1f} "
                f"{pass_pct:>6.1f}%"
            )

        lines.append("  " + "-" * 83)
        if self.total_calls > 0:
            lines.append(f"  {'TOTAL':<30} {self.total_calls:>6} {self.total_time_ms / self.total_calls:>8.1f}")

        lines.append("=" * 60)
        return "\n".join(lines)

    def to_json(self) -> str:
        """序列化为 JSON"""
        data = {
            "total_calls": self.total_calls,
            "total_time_ms": self.total_time_ms,
            "elapsed_sec": self.elapsed_sec,
            "gates": {},
        }
        for name, stats in self.gate_stats.items():
            data["gates"][name] = {
                "calls": stats.calls,
                "total_ms": stats.total_ms,
                "avg_ms": stats.avg_ms,
                "min_ms": stats.min_ms if stats.min_ms != float("inf") else 0.0,
                "max_ms": stats.max_ms,
                "p50_ms": stats.p50_ms,
                "p95_ms": stats.p95_ms,
                "p99_ms": stats.p99_ms,
                "passed": stats.passed_count,
                "failed": stats.failed_count,
            }
        return json.dumps(data, indent=2)


class Profiler:
    """
    性能剖析器。

    生命周期: start() → record_gate()* → stop() → report()

    线程安全：非线程安全，应在单线程事件循环中使用。
    """

    def __init__(self, name: str = "default"):
        self.name = name
        self._started: bool = False
        self._start_time: float = 0.0
        self._stop_time: float = 0.0
        self.records: list[ProfileRecord] = []

    def start(self) -> None:
        """开始 profiling"""
        self._started = True
        self._start_time = time.time()
        self.records = []

    def stop(self) -> None:
        """停止 profiling"""
        self._started = False
        self._stop_time = time.time()

    def record_gate(
        self,
        gate_name: str,
        duration_ms: float,
        passed: bool | None = None,
    ) -> None:
        """
        记录单次 gate 执行。

        如果 profiler 未启动，此调用为 no-op。
        """
        if not self._started:
            return
        self.records.append(
            ProfileRecord(
                gate_name=gate_name,
                duration_ms=duration_ms,
                passed=passed,
                timestamp=time.time(),
            )
        )

    def report(self) -> ProfileReport:
        """生成性能报告"""
        report = ProfileReport(
            _start_time=self._start_time,
            _stop_time=self._stop_time,
            elapsed_sec=(
                self._stop_time - self._start_time
                if self._stop_time > 0
                else time.time() - self._start_time
                if self._start_time > 0
                else 0.0
            ),
        )

        # 按 gate 汇总
        for rec in self.records:
            stats = report.gate_stats.get(rec.gate_name)
            if stats is None:
                stats = GateStats(gate_name=rec.gate_name)
                report.gate_stats[rec.gate_name] = stats

            stats.calls += 1
            stats.total_ms += rec.duration_ms
            stats.min_ms = min(stats.min_ms, rec.duration_ms)
            stats.max_ms = max(stats.max_ms, rec.duration_ms)
            stats.durations.append(rec.duration_ms)

            if rec.passed is True:
                stats.passed_count += 1
            elif rec.passed is False:
                stats.failed_count += 1

        report.total_calls = len(self.records)
        report.total_time_ms = sum(r.duration_ms for r in self.records)

        return report


@contextmanager
def gate_profile(profiler: Profiler, gate_name: str):
    """
    Context manager：自动计时 gate 执行。

    用法:
        with gate_profile(profiler, "gate1_execution") as ctx:
            result = run_gate1(improvement)
            ctx["passed"] = result.passed
    """
    ctx: dict[str, Any] = {}
    start = time.perf_counter()
    try:
        yield ctx
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        passed = ctx.get("passed")
        profiler.record_gate(gate_name, duration_ms, passed)


def install_middleware_profiler(app: Any, profiler: Profiler) -> None:
    """
    将 Profiler 安装到 FastAPI app 的 middleware 中。

    这会添加一个 HTTP middleware，自动记录每个请求中 quality gate
    的执行时间（在请求 state 中设置 profiler）。

    用法:
        from agent_prod.testing.profiling import Profiler, install_middleware_profiler
        profiler = Profiler()
        install_middleware_profiler(app, profiler)

    注意: 实际的 gate 计时需要在 gateway.validate 中集成 gate_profile。
          此 middleware 只是将 profiler 实例注入到请求上下文中。
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    class _ProfilerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # 将 profiler 注入到 request.state
            request.state.profiler = profiler
            response = await call_next(request)
            return response

    app.add_middleware(_ProfilerMiddleware)
