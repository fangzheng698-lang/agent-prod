"""Phase 7.4: GateStress — 并发压力测试。

GateStressRunner runs concurrent gate validations against the pipeline
to verify stability under load and detect regressions before release.

Usage:
    from agent_prod.testing.gate_stress import GateStressRunner

    runner = GateStressRunner()
    report = await runner.stress_test(gateway, turns_list)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class StressSample:
    """Single stress test sample result."""
    session_id: str
    gate_pass: bool
    duration_ms: float
    error: str = ""


@dataclass
class StressReport:
    """Aggregated stress test report."""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    total_samples: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    avg_duration_ms: float = 0.0
    max_duration_ms: float = 0.0
    min_duration_ms: float = 0.0
    pass_rate: float = 0.0
    stable: bool = True
    samples: list[StressSample] = field(default_factory=list)
    concurrency: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "total_samples": self.total_samples,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "avg_duration_ms": round(self.avg_duration_ms, 2),
            "max_duration_ms": round(self.max_duration_ms, 2),
            "min_duration_ms": round(self.min_duration_ms, 2),
            "pass_rate": round(self.pass_rate, 4),
            "stable": self.stable,
            "concurrency": self.concurrency,
        }

    @property
    def summary(self) -> str:
        lines = [
            f"GateStress: {self.total_samples} samples @ concurrency={self.concurrency}",
            f"  Pass: {self.passed}/{self.total_samples} ({self.pass_rate:.1%})",
            f"  Avg: {self.avg_duration_ms:.0f}ms  Max: {self.max_duration_ms:.0f}ms  Min: {self.min_duration_ms:.0f}ms",
            f"  Stable: {'YES' if self.stable else 'NO'}",
        ]
        return "\n".join(lines)


class GateStressRunner:
    """并发门禁压力测试。

    Runs multiple gate validations concurrently to verify:
    1. Pipeline doesn't crash under load
    2. Gate results are consistent
    3. Latency remains within acceptable bounds
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 5,
        max_duration_ms_threshold: float = 5000.0,
        stability_max_stddev_pct: float = 50.0,
    ):
        self.max_concurrency = max_concurrency
        self.max_duration_ms_threshold = max_duration_ms_threshold
        self.stability_max_stddev_pct = stability_max_stddev_pct
        self._last_report: StressReport | None = None

    async def stress_test(
        self,
        gateway,
        turns_list: list[list],
        *,
        concurrency: int | None = None,
        session_prefix: str = "stress",
    ) -> StressReport:
        """Run concurrent gate validations.

        Args:
            gateway: QualityGateGateway instance
            turns_list: List of turn batches to validate
            concurrency: Max concurrent validations (default: self.max_concurrency)
            session_prefix: Prefix for generated session IDs

        Returns:
            StressReport with aggregated results
        """
        concurrency = concurrency or self.max_concurrency

        import time as _time

        semaphore = asyncio.Semaphore(concurrency)
        samples: list[StressSample] = []

        async def _validate_one(idx: int, turns: list) -> StressSample:
            session_id = f"{session_prefix}-{idx}"
            t0 = _time.monotonic()
            try:
                async with semaphore:
                    improvement, passed = await gateway.validate(
                        session_id=session_id,
                        messages=[],
                        turns=turns,
                    )
                elapsed = (_time.monotonic() - t0) * 1000
                return StressSample(
                    session_id=session_id,
                    gate_pass=passed,
                    duration_ms=elapsed,
                )
            except Exception as e:
                elapsed = (_time.monotonic() - t0) * 1000
                return StressSample(
                    session_id=session_id,
                    gate_pass=False,
                    duration_ms=elapsed,
                    error=str(e)[:200],
                )

        tasks = [_validate_one(i, turns) for i, turns in enumerate(turns_list)]
        samples = await asyncio.gather(*tasks)

        # Aggregate
        n = len(samples)
        if n == 0:
            return StressReport()

        passed = sum(1 for s in samples if s.gate_pass and not s.error)
        failed = sum(1 for s in samples if s.error)
        errored = sum(1 for s in samples if not s.gate_pass and not s.error)
        durations = [s.duration_ms for s in samples]

        avg_dur = sum(durations) / n
        max_dur = max(durations)
        min_dur = min(durations)

        # Stability check: no errors, all gates pass, latency within bounds
        has_errors = failed > 0
        all_pass = passed == n

        # Duration standard deviation as percentage of mean
        if avg_dur > 0:
            variance = sum((d - avg_dur) ** 2 for d in durations) / n
            stddev_pct = (variance ** 0.5) / avg_dur * 100
        else:
            stddev_pct = 0.0

        stable = (
            not has_errors
            and all_pass
            and max_dur <= self.max_duration_ms_threshold
            and stddev_pct <= self.stability_max_stddev_pct
        )

        report = StressReport(
            total_samples=n,
            passed=passed,
            failed=failed,
            errored=errored,
            avg_duration_ms=avg_dur,
            max_duration_ms=max_dur,
            min_duration_ms=min_dur,
            pass_rate=passed / n,
            stable=stable,
            samples=samples,
            concurrency=concurrency,
        )
        self._last_report = report
        return report

    async def stress_test_with_lb(
        self,
        gateway,
        turns_list: list[list],
        *,
        concurrency: int | None = None,
        ramp_up: bool = True,
        ramp_steps: int = 3,
    ) -> StressReport:
        """Load-balancing stress test with optional ramp-up.

        When ramp_up=True, starts with 1 concurrent request and increases
        to target concurrency over ramp_steps steps.
        """
        if not ramp_up or len(turns_list) <= 3:
            return await self.stress_test(
                gateway, turns_list, concurrency=concurrency,
            )

        target = concurrency or self.max_concurrency
        step_size = max(1, len(turns_list) // ramp_steps)
        all_samples: list[StressSample] = []

        for step in range(ramp_steps):
            step_concurrency = max(1, int(target * (step + 1) / ramp_steps))
            chunk = turns_list[step * step_size : (step + 1) * step_size]
            if not chunk:
                continue
            report = await self.stress_test(
                gateway, chunk, concurrency=step_concurrency,
                session_prefix=f"stress-r{step}",
            )
            all_samples.extend(report.samples)

        # Re-aggregate
        n = len(all_samples)
        passed = sum(1 for s in all_samples if s.gate_pass and not s.error)
        failed = sum(1 for s in all_samples if s.error)
        durations = [s.duration_ms for s in all_samples]

        return StressReport(
            total_samples=n,
            passed=passed,
            failed=failed,
            errored=n - passed - failed,
            avg_duration_ms=sum(durations) / n if n else 0,
            max_duration_ms=max(durations) if durations else 0,
            min_duration_ms=min(durations) if durations else 0,
            pass_rate=passed / n if n else 0,
            stable=(failed == 0 and passed == n),
            samples=all_samples,
            concurrency=target,
        )

    @property
    def last_report(self) -> StressReport | None:
        return self._last_report
