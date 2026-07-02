"""Adaptive Gates — 动态阈值替代固定常量。

Evolution: 固定阈值 (1.2, 0.95, 50) → EWMA + 标准差带 → 自适应门禁。

核心机制:
1. Rolling window: 保留最近 N 个执行指标
2. EWMA: 指数加权移动平均追踪趋势
3. σ-band: μ ± k·σ 动态上下界
4. Calibrate: 基线固化（用户手动 lock-in 好的基线）
5. Evaluate: 新执行是否落在动态带内

用法:
    engine = AdaptiveGateEngine("gate1", ["duration_ms", "tokens"])
    engine.record_metric("duration_ms", 2100)
    engine.calibrate_all()
    result = engine.evaluate({"duration_ms": 2500, "tokens": 600})
    print(result["passed"], result["violations"])
"""

from __future__ import annotations

import math
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

# ═══════════════════════════════════════════
# Statistical helpers
# ═══════════════════════════════════════════

def _mean_ewma(prev: float, new: float, alpha: float) -> float:
    """单步 EWMA 更新。"""
    return alpha * new + (1 - alpha) * prev


def _std_ewma(prev_std: float, prev_mean: float, new_mean: float,
              new_val: float, alpha: float) -> float:
    """EWMA 方差的近似更新（Holton's method）。"""
    diff = new_val - prev_mean
    incr = alpha * diff * diff
    return math.sqrt(abs((1 - alpha) * (prev_std ** 2) + incr))


# ═══════════════════════════════════════════
# Models
# ═══════════════════════════════════════════

class AdaptiveThreshold(BaseModel):
    """单指标自适应阈值。"""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    alpha: float = 0.3
    sigma_mult: float = 2.0           # μ ± k·σ
    window_size: int = 50             # 滚动窗口大小
    min_samples: int = 10             # 最少样本数才校准

    # 运行时状态
    samples: deque[float] = Field(default_factory=deque)
    mean: float = 0.0
    std: float = 0.0
    sample_count: int = 0

    def observe(self, value: float):
        """记录一个新观测值。"""
        self.samples.append(value)
        if len(self.samples) > self.window_size:
            self.samples.popleft()
        self.sample_count += 1

        # 实时 EWMA 更新
        if self.mean == 0.0:
            self.mean = value
            self.std = 0.0
        else:
            old_mean = self.mean
            self.mean = _mean_ewma(self.mean, value, self.alpha)
            self.std = _std_ewma(self.std, old_mean, self.mean, value, self.alpha)

    @property
    def is_calibrated(self) -> bool:
        return self.sample_count >= self.min_samples and self.std > 0

    @property
    def upper(self) -> float:
        return self.mean + self.sigma_mult * max(self.std, 0.1)

    @property
    def lower(self) -> float:
        return max(0, self.mean - self.sigma_mult * max(self.std, 0.1))

    def calibrate(self):
        """基于当前窗口重新计算均值和标准差（批量校准）。"""
        if len(self.samples) < self.min_samples:
            return
        vals = list(self.samples)
        self.mean = sum(vals) / len(vals)
        if len(vals) > 1:
            self.std = math.sqrt(sum((v - self.mean) ** 2 for v in vals) / (len(vals) - 1))
        else:
            self.std = 0.0

    def check(self, value: float) -> bool:
        """检查值是否在动态带内。"""
        if not self.is_calibrated:
            return True  # 样本不够时放行
        return self.lower <= value <= self.upper


class GateThreshold(BaseModel):
    """门禁阈值 — 兼容固定/自适应两种模式。"""
    name: str = ""
    threshold_type: str = "fixed"    # fixed | adaptive
    fixed_value: float = 0.0
    ewma_mean: float = 0.0
    ewma_std: float = 0.0
    sigma_mult: float = 2.0

    @property
    def is_fixed(self) -> bool:
        return self.threshold_type == "fixed"

    @property
    def adaptive_upper(self) -> float:
        return self.ewma_mean + self.sigma_mult * max(self.ewma_std, 0.1)

    @property
    def adaptive_lower(self) -> float:
        return max(0, self.ewma_mean - self.sigma_mult * max(self.ewma_std, 0.1))

    @property
    def display(self) -> str:
        if self.is_fixed:
            return f"fixed={self.fixed_value}"
        return f"μ={self.ewma_mean:.1f}±{self.sigma_mult}σ=[{self.adaptive_lower:.1f}, {self.adaptive_upper:.1f}]"

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict) -> GateThreshold:
        return cls(**d)


# ═══════════════════════════════════════════
# Adaptive Gate Engine
# ═══════════════════════════════════════════

class AdaptiveGateEngine:
    """单道门的自适应引擎。

    用法:
        engine = AdaptiveGateEngine("gate1_execution", ["duration_ms", "tokens"])
        engine.record_metric("duration_ms", 2100)
        engine.calibrate_all()
        result = engine.evaluate({"duration_ms": 2500, "tokens": 600})
    """

    def __init__(
        self,
        gate_name: str,
        metrics: list[str],
        window_size: int = 50,
        sigma_mult: float = 2.0,
        min_samples: int = 10,
        alpha: float = 0.3,
    ):
        self.gate_name = gate_name
        self._thresholds: dict[str, AdaptiveThreshold] = {}
        for m in metrics:
            self._thresholds[m] = AdaptiveThreshold(
                alpha=alpha,
                sigma_mult=sigma_mult,
                window_size=window_size,
                min_samples=min_samples,
            )

    def record_metric(self, metric_name: str, value: float):
        """记录一次执行的某个指标值。"""
        if metric_name in self._thresholds:
            self._thresholds[metric_name].observe(value)

    def calibrate_all(self):
        """全部指标批量校准。"""
        for t in self._thresholds.values():
            t.calibrate()

    def evaluate(self, metrics: dict[str, float]) -> dict:
        """评估一组指标是否通过。

        返回: {"passed": bool, "violations": [...], "details": {...}}
        """
        violations = []
        details = {}

        for name, at in self._thresholds.items():
            value = metrics.get(name)
            if value is None:
                continue

            ok = at.check(value)
            details[name] = {
                "value": value,
                "passed": ok,
                "upper": at.upper,
                "lower": at.lower,
                "mean": at.mean,
                "std": at.std,
            }
            if not ok:
                violations.append({
                    "metric": name,
                    "value": value,
                    "upper": at.upper,
                    "lower": at.lower,
                    "deviation": (value - at.mean) / max(at.std, 0.1),
                })

        return {
            "passed": len(violations) == 0,
            "violations": violations,
            "details": details,
        }

    def get_thresholds(self) -> dict[str, GateThreshold]:
        """导出当前阈值快照。"""
        result = {}
        for name, at in self._thresholds.items():
            result[name] = GateThreshold(
                name=name,
                threshold_type="adaptive",
                ewma_mean=at.mean,
                ewma_std=at.std,
                sigma_mult=at.sigma_mult,
            )
        return result


class MultiGateAdaptiveEngine:
    """多道门自适应引擎。

    用法:
        mge = MultiGateAdaptiveEngine()
        mge.add_gate("execution", ["duration_ms", "tokens"])
        mge.add_gate("regression", ["duration_ms"])
        mge.record("execution", {"duration_ms": 2100, "tokens": 500})
        mge.calibrate_all()
        result = mge.evaluate_all({...})
    """

    def __init__(self):
        self._gates: dict[str, AdaptiveGateEngine] = {}

    def add_gate(
        self,
        name: str,
        metrics: list[str],
        window_size: int = 50,
        sigma_mult: float = 2.0,
        min_samples: int = 10,
    ):
        self._gates[name] = AdaptiveGateEngine(
            name, metrics, window_size, sigma_mult, min_samples
        )

    def record(self, gate_name: str, metrics: dict[str, float]):
        if gate_name in self._gates:
            eng = self._gates[gate_name]
            for k, v in metrics.items():
                eng.record_metric(k, v)

    def calibrate_all(self):
        for eng in self._gates.values():
            eng.calibrate_all()

    def evaluate_all(self, gate_metrics: dict[str, dict[str, float]]) -> dict:
        """评估所有门禁。

        gate_metrics: {"execution": {"duration_ms": 1000, "tokens": 500}, ...}
        返回: {"all_passed": bool, "results": {...}, "failed_gates": [...]}
        """
        results = {}
        failed_gates = []

        for gname, eng in self._gates.items():
            metrics = gate_metrics.get(gname, {})
            r = eng.evaluate(metrics)
            results[gname] = r
            if not r["passed"]:
                failed_gates.append(gname)

        return {
            "all_passed": len(failed_gates) == 0,
            "results": results,
            "failed_gates": failed_gates,
        }


# ═══════════════════════════════════════════
# Free functions
# ═══════════════════════════════════════════

def compute_adaptive_threshold(
    samples: list[float],
    sigma_mult: float = 2.0,
) -> GateThreshold:
    """从样本列表计算自适应阈值。"""
    if len(samples) < 2:
        return GateThreshold(
            threshold_type="adaptive",
            ewma_mean=samples[0] if samples else 0,
            ewma_std=0,
            sigma_mult=sigma_mult,
        )
    mean = sum(samples) / len(samples)
    std = math.sqrt(sum((v - mean) ** 2 for v in samples) / (len(samples) - 1))
    return GateThreshold(
        threshold_type="adaptive",
        ewma_mean=mean,
        ewma_std=std,
        sigma_mult=sigma_mult,
    )


def is_within_band(value: float, threshold: GateThreshold | AdaptiveThreshold) -> bool:
    """检查值是否在动态带内。"""
    if isinstance(threshold, GateThreshold):
        if threshold.is_fixed:
            return True  # fixed thresholds handled elsewhere
        return threshold.adaptive_lower <= value <= threshold.adaptive_upper
    else:
        return threshold.check(value)
