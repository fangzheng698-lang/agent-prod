"""
Gate3: 回归验证门
核心：用 DeepDiff 做结构化 diff + 数值指标降级检测
Phase 1: 阈值从 YAML 加载 + 异常保护
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .models import GateResult, GateName, Improvement, RollbackLevel, RollbackPlan

logger = logging.getLogger(__name__)

try:
    from deepdiff import DeepDiff
    _DEEPDIFF_AVAILABLE = True
except ImportError:
    _DEEPDIFF_AVAILABLE = False
    DeepDiff = None  # type: ignore


class Regression(BaseModel):
    """单条回归检测结果"""
    field: str
    old_value: Any = None
    new_value: Any = None
    change_type: str = ""  # changed / added / removed / type_change / degraded
    severity: str = "info"  # info / warning / critical


class Gate3Config(BaseModel):
    regress_pct: float = 0.95           # 不低于基线的 95%
    perf_degradation_limit: float = 0.05  # 性能退化不超过 5%
    repeatability_threshold: float = 0.1
    repeatability_runs: int = 3
    unstable_retry_count: int = 5

    @classmethod
    def from_yaml(cls, data: dict | None) -> "Gate3Config":
        if not data:
            return cls()
        gate_cfg = data.get("gates", {}).get("gate3", {})
        return cls(**{k: v for k, v in gate_cfg.items()
                       if k in cls.model_fields})


class Gate3Regression:
    """回归验证门 — 用 DeepDiff 做结构化对比"""

    def __init__(self, config: Gate3Config | None = None):
        self.config = config or Gate3Config()

    def verify(self, improvement: Improvement) -> GateResult:
        """执行 Gate3 验证"""
        start = time.time()

        if not improvement.baseline_output:
            return GateResult(
                gate_name=GateName.GATE3,
                passed=True,
                reason="No baseline — first run, skipping regression",
                details={"skipped": True},
                duration_ms=(time.time() - start) * 1000,
            )

        if _DEEPDIFF_AVAILABLE:
            return self._verify_deepdiff(improvement, start)
        else:
            return self._verify_fallback(improvement, start)

    def _verify_deepdiff(self, improvement: Improvement, start: float) -> GateResult:
        """完整 DeepDiff 对比"""
        diff = DeepDiff(
            improvement.baseline_output,
            improvement.candidate_output,
            significant_digits=3,
            exclude_paths=["__timestamp__", "__runtime_ms__"],
            ignore_order=True,
            report_repetition=True,
        )

        regressions: list[Regression] = []

        for category in ["values_changed", "type_changes",
                         "iterable_item_added", "iterable_item_removed",
                         "repetition_changed"]:
            changes = getattr(diff, category, {}) or {}
            severity = "critical" if category in ("values_changed", "type_changes") else "warning"
            for path, detail in changes.items():
                regressions.append(Regression(
                    field=str(path),
                    old_value=detail.get("old_value", detail),
                    new_value=detail.get("new_value", None),
                    change_type=category,
                    severity=severity,
                ))

        # 关键数值指标降级检查
        for key in ("f1_score", "accuracy", "success_rate", "bleu", "rouge_l"):
            regressions.extend(self._check_metric_degradation(key, improvement))

        # 性能降级
        regressions.extend(self._check_perf_degradation(improvement))

        critical = [r for r in regressions if r.severity == "critical"]
        passed = len(critical) == 0

        return GateResult(
            gate_name=GateName.GATE3,
            passed=passed,
            reason=(
                "No critical regressions"
                if passed
                else f"{len(critical)} critical regression(s): "
                     f"{', '.join(r.field for r in critical[:3])}"
            ),
            details={
                "total_diffs": len(diff) if diff else 0,
                "regressions": [r.model_dump() for r in regressions],
                "critical_count": len(critical),
                "warning_count": len([r for r in regressions if r.severity == "warning"]),
            },
            duration_ms=(time.time() - start) * 1000,
        )

    def _verify_fallback(self, improvement: Improvement, start: float) -> GateResult:
        """降级方案：数值指标对比（零依赖）"""
        baseline, candidate = improvement.baseline_output, improvement.candidate_output
        regressions: list[Regression] = []

        for key in baseline:
            if key not in candidate:
                regressions.append(Regression(field=key, change_type="missing", severity="critical"))
                continue
            bv, cv = baseline[key], candidate[key]
            if isinstance(bv, (int, float)) and isinstance(cv, (int, float)):
                if bv > 0 and cv < bv * self.config.regress_pct:
                    regressions.append(Regression(
                        field=key,
                        old_value=bv, new_value=cv,
                        change_type=f"degraded {(cv/bv)*100:.0f}% of baseline",
                        severity="critical",
                    ))

        passed = len(regressions) == 0
        return GateResult(
            gate_name=GateName.GATE3,
            passed=passed,
            reason="No regressions (manual compare)" if passed
                   else "; ".join(f"{r.field}: {r.change_type}" for r in regressions[:5]),
            details={"regressions": [r.model_dump() for r in regressions]},
            duration_ms=(time.time() - start) * 1000,
        )

    def _check_metric_degradation(self, key: str, imp: Improvement) -> list[Regression]:
        bv = imp.baseline_output.get(key)
        cv = imp.candidate_output.get(key)
        if bv is None or cv is None or not isinstance(bv, (int, float)):
            return []
        if bv > 0 and cv < bv * self.config.regress_pct:
            return [Regression(
                field=key, old_value=bv, new_value=cv,
                change_type=f"degraded {((cv - bv) / bv) * 100:+.1f}%",
                severity="critical",
            )]
        return []

    def _check_perf_degradation(self, imp: Improvement) -> list[Regression]:
        old = imp.baseline_output.get("latency_ms", 0)
        new = imp.candidate_output.get("latency_ms", 0)
        if not old or not new:
            return []
        if new > old * (1 + self.config.perf_degradation_limit):
            return [Regression(
                field="latency_ms", old_value=old, new_value=new,
                change_type=f"degraded +{((new - old) / old) * 100:.0f}%",
                severity="warning",
            )]
        return []

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L3 回滚：恢复 Benchmark 快照"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L3,
            scope="restore benchmark snapshot",
            estimated_seconds=30,
            procedure="RESTORE FROM benchmark_snapshot WHERE improvement_id = ?",
            executed_at=datetime.now(timezone.utc),
            success=True,
        )
        if improvement.baseline_output:
            improvement.candidate_output = improvement.baseline_output.copy()

# ── GatePlugin registration ──────────────────────────────
from .interface import register_gate
from .models import GateName
register_gate(GateName.GATE3, Gate3Regression)

