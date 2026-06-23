"""
Phase 5.1: Gate Stress Harness — 批量门禁压力测试工具

用途: 对 Quality Gate pipeline 做并发压测，收集汇总统计数据。
不依赖真实 HTTP 服务器，直接调用 QualityGateGateway.validate()。

用法:
    runner = GateStressRunner(validator=my_validator, turns=..., messages=...)
    report = await runner.run(n_requests=100, concurrency=10)
    print(report.summary_text())
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


ValidatorFn = Callable[
    [str, list[dict], list[Any]],
    Awaitable[tuple[Any, bool]],
]
"""validator(session_id, messages, turns) -> (improvement, all_passed)"""


@dataclass
class GateStressReport:
    """单次压测的结果汇总"""

    total: int = 0
    passed: int = 0
    rejected: int = 0
    gate_failures: dict[str, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    duration_sec: float = 0.0

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.passed / self.total

    @property
    def fail_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.rejected / self.total

    @property
    def error_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.errors / self.total

    def compute_percentiles(self) -> tuple[float, float, float]:
        """返回 (p50, p95, p99) 延迟，单位 ms"""
        if not self.latencies:
            return (0.0, 0.0, 0.0)
        sorted_lat = sorted(self.latencies)
        n = len(sorted_lat)

        def _pct(p: float) -> float:
            idx = int(n * p)
            if idx >= n:
                idx = n - 1
            return sorted_lat[idx]

        return (_pct(0.50), _pct(0.95), _pct(0.99))

    def summary_text(self) -> str:
        """生成人类可读的汇总报告"""
        p50, p95, p99 = self.compute_percentiles()
        lines = [
            "=" * 60,
            "  GATE STRESS TEST REPORT",
            "=" * 60,
            f"  Total Requests:     {self.total}",
            f"  Passed:             {self.passed} ({self.pass_rate:.1%})",
            f"  Rejected:           {self.rejected} ({self.fail_rate:.1%})",
            f"  Errors:             {self.errors} ({self.error_rate:.1%})",
            f"  Duration:           {self.duration_sec:.2f}s",
            f"  Latency p50/p95/p99: {p50:.1f}ms / {p95:.1f}ms / {p99:.1f}ms",
        ]

        if self.gate_failures:
            lines.append("")
            lines.append("  Gate Failure Breakdown:")
            for gate_name, count in sorted(
                self.gate_failures.items(),
                key=lambda x: -x[1],
            ):
                rate = count / self.total * 100 if self.total else 0
                lines.append(f"    {gate_name}: {count} ({rate:.1f}%)")

        if self.latencies:
            lines.append("")
            lines.append(f"  Latency Stats (ms):")
            sorted_lat = sorted(self.latencies)
            lines.append(f"    min={sorted_lat[0]:.1f}  max={sorted_lat[-1]:.1f}")
            lines.append(f"    mean={statistics.mean(sorted_lat):.1f}  stdev={statistics.stdev(sorted_lat) if len(sorted_lat) > 1 else 0:.1f}")

        lines.append("=" * 60)
        return "\n".join(lines)


class GateStressRunner:
    """
    门禁压测执行器。

    用法:
        runner = GateStressRunner(
            validator=gw.validate,
            turns=[...],
            messages=[...],
        )
        report = await runner.run(n_requests=100, concurrency=10)
    """

    def __init__(
        self,
        validator: ValidatorFn,
        turns: list[Any],
        messages: list[dict],
    ):
        self._validator = validator
        self._turns = turns
        self._messages = messages
        self._session_counter = 0

    def _next_session_id(self) -> str:
        self._session_counter += 1
        return f"stress-{self._session_counter}"

    async def _single_request(
        self,
        semaphore: asyncio.Semaphore,
    ) -> dict:
        """执行单次门禁验证，返回 {'passed', 'latency_ms', 'failed_gate', 'error'}."""
        session_id = self._next_session_id()
        start = time.monotonic()

        async with semaphore:
            try:
                improvement, all_passed = await self._validator(
                    session_id, self._messages, self._turns,
                )
                latency = (time.monotonic() - start) * 1000

                failed_gate = None
                if not all_passed:
                    # Try to extract which gate failed
                    if hasattr(improvement, 'fail_gate') and improvement.fail_gate:
                        fg = improvement.fail_gate
                        if hasattr(fg, 'value'):
                            failed_gate = fg.value
                        else:
                            failed_gate = str(fg)

                return {
                    "passed": all_passed,
                    "latency_ms": latency,
                    "failed_gate": failed_gate,
                    "error": None,
                }
            except Exception as e:
                latency = (time.monotonic() - start) * 1000
                return {
                    "passed": False,
                    "latency_ms": latency,
                    "failed_gate": None,
                    "error": str(e),
                }

    async def run(self, n_requests: int = 100, concurrency: int = 10) -> GateStressReport:
        """
        执行压测。

        参数:
            n_requests: 总请求数
            concurrency: 最大并发数

        返回:
            GateStressReport 汇总结果
        """
        if n_requests <= 0:
            return GateStressReport()

        semaphore = asyncio.Semaphore(concurrency)

        start = time.monotonic()

        tasks = [self._single_request(semaphore) for _ in range(n_requests)]
        results = await asyncio.gather(*tasks)

        duration = time.monotonic() - start

        # 汇总
        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        errors = sum(1 for r in results if r["error"] is not None)
        rejected = total - passed - errors
        latencies = [r["latency_ms"] for r in results]

        # 按门统计失败
        gate_failures: dict[str, int] = {}
        for r in results:
            fg = r["failed_gate"]
            if fg:
                gate_failures[fg] = gate_failures.get(fg, 0) + 1

        return GateStressReport(
            total=total,
            passed=passed,
            rejected=rejected,
            gate_failures=gate_failures,
            latencies=latencies,
            errors=errors,
            duration_sec=duration,
        )


async def load_test(
    n_requests: int = 100,
    concurrency: int = 10,
    validator: ValidatorFn | None = None,
    turns: list[Any] | None = None,
    messages: list[dict] | None = None,
    server_url: str = "",
    prompt_variants: list[str] | None = None,
) -> GateStressReport:
    """
    便捷压测入口。

    当 validator 提供时，直接使用 validator（单元测试模式）。
    当 server_url 提供时，发送 HTTP 请求到 FastAPI 服务（集成测试模式）。

    参数:
        n_requests: 总请求数
        concurrency: 最大并发数
        validator: 门禁验证函数 (单元测试模式)
        turns: TurnRecord 列表
        messages: 消息列表
        server_url: API 服务器地址 (集成测试模式)
        prompt_variants: 不同提示词变体（用于 HTTP 模式）
    """
    if validator is not None:
        # 单元测试模式：直接调用 validator
        turns = turns or []
        messages = messages or []
        runner = GateStressRunner(
            validator=validator,
            turns=turns,
            messages=messages,
        )
        return await runner.run(
            n_requests=n_requests,
            concurrency=concurrency,
        )

    if server_url:
        # HTTP 集成测试模式（需要 aiohttp 或 httpx）
        raise NotImplementedError(
            "HTTP mode requires a running server. Use validator=... for unit test mode."
        )

    raise ValueError("Either validator or server_url must be provided")
