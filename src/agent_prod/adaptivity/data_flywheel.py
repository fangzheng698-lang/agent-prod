"""Data Flywheel — 统计基线 + EWMA 趋势 + 显著性检验。

从硬编码阈值进化到数据驱动的自适应优化：
- compute_baseline(): 从历史执行日志计算统计基线 (μ, σ, p50, p95)
- detect_trend():   线性回归检测趋势方向 (improving/stable/regressing)
- welch_t_test():    Welch t-test 判断新旧版本是否有显著差异
- FlywheelEngine:    串联基线建立 → 趋势检测 → 显著性验证 → 自动建议

用法:
    fw = FlywheelEngine("data/execution_log.jsonl")
    fw.log_execution(...)  # 每次执行后记录
    baseline = fw.establish_baseline(min_samples=20)
    report = fw.generate_report(recent_count=10)
    print(report.summary)
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from agent_prod.observability.execution_log import ExecutionLogRecord


# ═══════════════════════════════════════════
# Statistical Functions
# ═══════════════════════════════════════════

def _mean(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _std(vals: list[float], mu: float | None = None) -> float:
    if len(vals) < 2:
        return 0.0
    if mu is None:
        mu = _mean(vals)
    return math.sqrt(sum((x - mu) ** 2 for x in vals) / (len(vals) - 1))


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = k - f
    if f + 1 < len(s):
        return s[f] + c * (s[f + 1] - s[f])
    return s[f]


def ewma(values: list[float], alpha: float = 0.3) -> list[float]:
    """指数加权移动平均。"""
    if not values:
        return []
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def welch_t_test(
    group_a: list[float],
    group_b: list[float],
    alpha: float = 0.05,
) -> tuple[float, bool, float]:
    """Welch's t-test (不等方差)。

    返回: (p_value, is_significant, cohen_d_effect_size)
    """
    if len(group_a) < 2 or len(group_b) < 2:
        return 1.0, False, 0.0

    ma, mb = _mean(group_a), _mean(group_b)
    sa, sb = _std(group_a, ma), _std(group_b, mb)
    na, nb = len(group_a), len(group_b)

    var_a = sa**2 / na
    var_b = sb**2 / nb

    denom = math.sqrt(var_a + var_b)
    if denom < 1e-10:
        return 1.0, False, 0.0

    t_stat = (ma - mb) / denom

    # Welch-Satterthwaite degrees of freedom
    num = (var_a + var_b) ** 2
    denom_df = (var_a**2) / (na - 1) + (var_b**2) / (nb - 1)
    if denom_df < 1e-10:
        df = na + nb - 2
    else:
        df = num / denom_df

    # Approximate p-value from t-distribution
    p_value = _t_pvalue(abs(t_stat), df)

    # Cohen's d effect size
    pooled_sd = math.sqrt(((sa**2 * (na - 1) + sb**2 * (nb - 1)) / (na + nb - 2)) or 1)
    effect_size = abs(ma - mb) / pooled_sd

    return p_value, p_value < alpha, effect_size


def _t_pvalue(t: float, df: float) -> float:
    """t-distribution two-tailed p-value approximation."""
    # Using Abramowitz & Stegun approximation
    x = df / (df + t * t)
    # Regularized incomplete beta function approximation
    if df <= 1:
        return 2.0 * (1.0 - math.atan(t) / math.pi)
    # Simple approximation for df > 1
    a = df / 2.0
    b = 0.5
    # Use the relationship with beta incomplete
    import math
    p = _betai(a, b, x)
    return p


def _betai(a: float, b: float, x: float) -> float:
    """Incomplete beta function approximation via continued fractions."""
    if x < 0 or x > 1:
        return 0.0
    if x == 0 or x == 1:
        return 1.0 if x == 1 else 0.0
    # Use the continued fraction representation
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) +
                  a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    else:
        return 1 - bt * _betacf(b, a, 1 - x) / b


def _betacf(a: float, b: float, x: float, max_iter: int = 100) -> float:
    """Continued fraction for incomplete beta."""
    eps = 1e-10
    qab = a + b
    qap = a + 1
    qam = a - 1
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < eps:
        d = eps
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        del_ = d * c
        h *= del_
        if abs(del_ - 1.0) < eps:
            break
    return h


def detect_trend(
    records: list[ExecutionLogRecord],
    window_size: int = 10,
) -> TrendReport:
    """线性回归检测 token 效率趋势。"""
    n = len(records)
    if n < window_size:
        return TrendReport(direction="stable", slope=0.0, confidence=0.1,
                           description=f"Need {window_size} samples, have {n}")

    # 取最近 N 个样本的 token 消耗
    recent = records[-window_size:]
    tokens_per_turn = [
        (r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)) / max(1, r.turns)
        for r in recent
    ]

    # 线性回归: y = a + b*x，x = 0..n-1
    xs = list(range(window_size))
    x_mean = _mean(xs)
    y_mean = _mean(tokens_per_turn)

    num = sum((xs[i] - x_mean) * (tokens_per_turn[i] - y_mean) for i in range(window_size))
    den = sum((x - x_mean) ** 2 for x in xs)
    slope = num / den if den > 0 else 0.0

    # 归一化斜率到 [-1, 1]
    y_range = max(tokens_per_turn) - min(tokens_per_turn) or 1
    norm_slope = slope * (window_size - 1) / y_range
    norm_slope = max(-1.0, min(1.0, norm_slope))

    # t-test for slope significance
    if len(tokens_per_turn) >= 4:
        residuals = [tokens_per_turn[i] - (y_mean + slope * (xs[i] - x_mean)) for i in range(window_size)]
        se = math.sqrt(sum(r**2 for r in residuals) / (window_size - 2) / den) if den > 0 else 0
        if se > 0:
            t_slope = abs(slope) / se
            # rough confidence from t-statistic
            confidence = min(1.0, t_slope / 2.0)
        else:
            confidence = 0.99
    else:
        confidence = 0.5

    if norm_slope < -0.05 and confidence > 0.5:
        direction = "improving"   # tokens are decreasing
    elif norm_slope > 0.05 and confidence > 0.5:
        direction = "regressing"  # tokens are increasing
    else:
        direction = "stable"

    return TrendReport(
        direction=direction,
        slope=round(norm_slope, 4),
        confidence=round(confidence, 2),
        description=f"Token-per-turn trend: {direction} (slope={norm_slope:.4f})",
    )


# ═══════════════════════════════════════════
# Models
# ═══════════════════════════════════════════

class StatBaseline(BaseModel):
    """统计基线 — μ + σ + 分位数。"""
    avg_tokens: float = 0.0
    token_std: float = 0.0
    token_p50: float = 0.0
    token_p95: float = 0.0
    avg_duration_ms: float = 0.0
    duration_std: float = 0.0
    duration_p50: float = 0.0
    duration_p95: float = 0.0
    avg_turns: float = 0.0
    turns_std: float = 0.0
    gate_pass_rate: float = 0.0
    sample_count: int = 0

    @property
    def token_upper_bound(self) -> float:
        return self.avg_tokens + 2 * self.token_std

    @property
    def duration_upper_bound(self) -> float:
        return self.avg_duration_ms + 2 * self.duration_std

    def to_dict(self) -> dict:
        return self.model_dump()


class TrendReport(BaseModel):
    """趋势检测报告。"""
    direction: str = "stable"  # improving, stable, regressing
    slope: float = 0.0
    confidence: float = 0.0
    description: str = ""


class FlywheelSuggestion(BaseModel):
    """数据驱动的优化建议。"""
    category: str = ""
    severity: str = "info"
    title: str = ""
    description: str = ""
    current_value: str = ""
    suggested_value: str = ""
    confidence: float = 0.5


class FlywheelReport(BaseModel):
    """飞轮完整报告。"""
    summary: str = ""
    improvement_detected: bool = False
    confidence: float = 0.0
    baseline: StatBaseline | None = None
    trend: TrendReport = Field(default_factory=TrendReport)
    suggestions: list[FlywheelSuggestion] = Field(default_factory=list)
    p_value: float = 1.0
    effect_size: float = 0.0

    def to_dict(self) -> dict:
        d = self.model_dump()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FlywheelReport":
        return cls(**d)


# ═══════════════════════════════════════════
# Flywheel Engine
# ═══════════════════════════════════════════

class FlywheelEngine:
    """数据飞轮引擎：记录 → 基线 → 趋势 → 显著性 → 建议。"""

    def __init__(self, log_path: str):
        self._log_path = log_path
        self._memory_logs: list[ExecutionLogRecord] = []
        if log_path != ":memory:":
            os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)

    def log_execution(
        self,
        run_id: str,
        session_id: str,
        prompt: str,
        response: str,
        turns: int,
        tokens: int,
        duration_ms: float,
        gate_pass: bool,
        gate_status: str,
    ):
        """记录一次执行。"""
        record = ExecutionLogRecord(
            run_id=run_id,
            session_id=session_id,
            prompt=prompt,
            response=response,
            turns=turns,
            costs={"prompt_tokens": tokens // 2, "completion_tokens": tokens - tokens // 2},
            duration_ms=duration_ms,
            quality_gate_result={"status": gate_status, "passed": gate_pass},
        )
        if self._log_path == ":memory:":
            self._memory_logs.append(record)
            return

        with open(self._log_path, "a") as f:
            f.write(record.model_dump_json() + "\n")

    def _load_logs(self, limit: int = 500) -> list[ExecutionLogRecord]:
        if self._log_path == ":memory:":
            return list(getattr(self, "_memory_logs", []))
        records = []
        if not os.path.exists(self._log_path):
            return records
        with open(self._log_path, "r") as f:
            for line in f:
                try:
                    records.append(ExecutionLogRecord.model_validate_json(line.strip()))
                except Exception:
                    pass
        return records[-limit:]

    def establish_baseline(self, min_samples: int = 10) -> StatBaseline | None:
        """从历史日志建立统计基线。"""
        logs = self._load_logs()
        if len(logs) < min_samples:
            return None
        d = compute_baseline(logs)
        return StatBaseline(**d)

    def generate_report(self, recent_count: int = 10) -> FlywheelReport:
        """生成完整飞轮报告：基线 → 趋势 → 显著性 → 建议。"""
        all_logs = self._load_logs()
        if len(all_logs) < 5:
            return FlywheelReport(summary="Need at least 5 executions to generate report",
                                  improvement_detected=False, confidence=0.0)

        # 1. 基线
        baseline = compute_baseline(all_logs)
        bl = StatBaseline(**baseline)

        # 2. 趋势
        trend = detect_trend(all_logs)

        # 3. 显著性：比较前 N 和最后 N 个的执行时长
        split = max(len(all_logs) // 2, recent_count)
        old_logs = all_logs[:split]
        new_logs = all_logs[-recent_count:]

        old_durations = [float(r.duration_ms) for r in old_logs]
        new_durations = [float(r.duration_ms) for r in new_logs]

        old_tokens = [float(r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)) for r in old_logs]
        new_tokens = [float(r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)) for r in new_logs]

        p_time, sig_time, es_time = welch_t_test(old_durations, new_durations)
        p_token, sig_token, es_token = welch_t_test(old_tokens, new_tokens)

        # 4. 判断改进 — Welch t-test 是主要证据，趋势是辅助
        improved = (sig_time or sig_token)
        confidence = min(1.0, max(trend.confidence, (es_time + es_token) / 2))

        # 5. 生成建议
        suggestions = self._generate_suggestions(bl, old_logs, new_logs, sig_time, sig_token)

        summary_parts = []
        if sig_time:
            pct = _mean(new_durations) / max(_mean(old_durations), 1) - 1
            summary_parts.append(f"Duration {abs(pct):.0%} {'faster' if pct < 0 else 'slower'} (p={p_time:.4f})")
        if sig_token:
            pct = _mean(new_tokens) / max(_mean(old_tokens), 1) - 1
            summary_parts.append(f"Tokens {abs(pct):.0%} {'less' if pct < 0 else 'more'} (p={p_token:.4f})")
        if not summary_parts:
            summary_parts.append("No significant change detected")

        return FlywheelReport(
            summary="; ".join(summary_parts),
            improvement_detected=improved,
            confidence=round(confidence, 2),
            baseline=bl,
            trend=trend,
            suggestions=suggestions,
            p_value=round(min(p_time, p_token), 4),
            effect_size=round(max(es_time, es_token), 2),
        )

    def _generate_suggestions(
        self,
        baseline: StatBaseline,
        old_logs: list[ExecutionLogRecord],
        new_logs: list[ExecutionLogRecord],
        sig_time: bool,
        sig_token: bool,
    ) -> list[FlywheelSuggestion]:
        """基于统计证据生成优化建议。"""
        suggestions = []
        new_dur = _mean([float(r.duration_ms) for r in new_logs])
        new_tok = _mean([float(r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)) for r in new_logs])

        # Token 超基线 2σ
        if new_tok > baseline.token_upper_bound:
            suggestions.append(FlywheelSuggestion(
                category="token_usage", severity="warning",
                title="Token usage exceeds baseline 2σ bound",
                description=f"Current avg {new_tok:.0f} tokens > baseline upper bound {baseline.token_upper_bound:.0f} (μ+2σ)",
                current_value=f"{new_tok:.0f} tokens",
                suggested_value=f"≤ {baseline.token_upper_bound:.0f} tokens",
                confidence=0.8,
            ))

        # 耗时超基线
        if new_dur > baseline.duration_upper_bound:
            suggestions.append(FlywheelSuggestion(
                category="time_efficiency", severity="warning",
                title="Duration exceeds baseline 2σ bound",
                description=f"Current avg {new_dur:.0f}ms > baseline upper bound {baseline.duration_upper_bound:.0f}ms",
                current_value=f"{new_dur:.0f}ms",
                suggested_value=f"≤ {baseline.duration_upper_bound:.0f}ms",
                confidence=0.8,
            ))

        # 显著改进（反向建议：锁定优化）
        if sig_time and new_dur < _mean([float(r.duration_ms) for r in old_logs]):
            suggestions.append(FlywheelSuggestion(
                category="optimization", severity="info",
                title="Significant time improvement detected — consider locking config",
                description=f"p={0.01:.4f}, effect_size={_mean([float(r.duration_ms) for r in old_logs]) / max(new_dur, 1) - 1:.0%} reduction",
                current_value=f"{new_dur:.0f}ms",
                suggested_value="Update baseline to current values",
                confidence=0.85,
            ))

        if sig_token and new_tok < _mean([float(r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)) for r in old_logs]):
            suggestions.append(FlywheelSuggestion(
                category="optimization", severity="info",
                title="Significant token efficiency improvement — update baseline",
                description=f"Tokens reduced, consider saving as new baseline",
                current_value=f"{new_tok:.0f} tokens",
                suggested_value="Run establish_baseline() to lock in gains",
                confidence=0.85,
            ))

        return suggestions


def compute_baseline(records: list[ExecutionLogRecord]) -> dict:
    """从历史记录计算统计基线。"""
    if not records:
        return {"sample_count": 0, "avg_tokens": 0}

    tokens = []
    durations = []
    turns_list = []
    gates_pass = 0

    for r in records:
        t = r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)
        tokens.append(float(t))
        durations.append(float(r.duration_ms))
        turns_list.append(float(r.turns))
        if r.quality_gate_result.get("passed", False):
            gates_pass += 1

    baseline = StatBaseline(
        avg_tokens=_mean(tokens),
        token_std=_std(tokens),
        token_p50=_percentile(tokens, 50),
        token_p95=_percentile(tokens, 95),
        avg_duration_ms=_mean(durations),
        duration_std=_std(durations),
        duration_p50=_percentile(durations, 50),
        duration_p95=_percentile(durations, 95),
        avg_turns=_mean(turns_list),
        turns_std=_std(turns_list),
        gate_pass_rate=gates_pass / len(records) if records else 0.0,
        sample_count=len(records),
    )
    return baseline.to_dict()
