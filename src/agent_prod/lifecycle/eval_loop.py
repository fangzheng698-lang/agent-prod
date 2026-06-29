"""Phase 6.2: Evaluation Loop — 自动评估改进效果。

比较两个版本 (baseline vs candidate) 的执行结果，计算效率得分和判定。

用法:
    from agent_prod.lifecycle.eval_loop import compare_versions, EvalReport

    report = compare_versions(baseline_logs, candidate_logs, "v1.0", "v2.0")
    print(report.verdict)  # "improved" | "regressed" | "neutral"
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_prod.observability.execution_log import ExecutionLogger, ExecutionLogRecord


class EvalReport(BaseModel):
    """版本对比评估报告。"""
    baseline_name: str = "baseline"
    candidate_name: str = "candidate"
    baseline_count: int = 0
    candidate_count: int = 0
    token_efficiency: float = 0.0       # 正数=改进, 负数=退化
    time_efficiency: float = 0.0        # 正数=改进, 负数=退化
    gate_pass_rate_change: float = 0.0  # 正数=提升
    quality_score: float = 0.0          # 0~1 综合质量分
    verdict: str = "neutral"            # improved | regressed | neutral
    details: list[str] = Field(default_factory=list)


class EvaluationLoop:
    """评估循环——加载日志并对比两个版本。"""

    def __init__(self, logger: ExecutionLogger | None = None):
        self._logger = logger or ExecutionLogger()

    def load_logs(self, session_id: str | None = None) -> list[ExecutionLogRecord]:
        """从日志加载执行记录。"""
        return self._logger.query_log(session_id=session_id)

    def compare(
        self,
        baseline_logs: list[ExecutionLogRecord],
        candidate_logs: list[ExecutionLogRecord],
        baseline_name: str = "baseline",
        candidate_name: str = "candidate",
    ) -> EvalReport:
        """比较两个版本的执行结果。"""
        return compare_versions(baseline_logs, candidate_logs, baseline_name, candidate_name)


def _avg_total_tokens(logs: list[ExecutionLogRecord]) -> float:
    """计算平均 token 消耗 (prompt + completion)。"""
    if not logs:
        return 0.0
    total = sum(
        r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)
        for r in logs
    )
    return total / len(logs)


def _avg_duration_ms(logs: list[ExecutionLogRecord]) -> float:
    """计算平均执行时间。"""
    if not logs:
        return 0.0
    return sum(r.duration_ms for r in logs) / len(logs)


def _gate_pass_rate(logs: list[ExecutionLogRecord]) -> float:
    """计算门禁通过率。"""
    if not logs:
        return 0.0
    passed = sum(1 for r in logs if r.quality_gate_result.get("passed", False))
    return passed / len(logs)


def _compute_quality_score(
    token_eff: float,
    time_eff: float,
    gate_change: float,
) -> float:
    """综合质量分 0~1。

    使用 sigmoid 将各指标映射到 [0,1] 后加权平均。
    """
    def sigmoid(x: float) -> float:
        # 将 [-inf, inf] 映射到 (0, 1)
        import math
        try:
            return 1.0 / (1.0 + math.exp(-x))
        except OverflowError:
            return 1.0 if x > 0 else 0.0

    # 归一化各维度分 (efficiency 通常 -1~1，取 sigmoid * 2 - 1 再 clamp)
    token_score = max(0.0, min(1.0, (sigmoid(token_eff * 3) - 0.5) * 2 + 0.5))
    time_score = max(0.0, min(1.0, (sigmoid(time_eff * 3) - 0.5) * 2 + 0.5))
    gate_score = max(0.0, min(1.0, sigmoid(gate_change * 10) * 0.8 + 0.2))

    # 加权平均
    score = token_score * 0.35 + time_score * 0.25 + gate_score * 0.40
    return round(min(max(score, 0.0), 1.0), 4)


def compare_versions(
    baseline_logs: list[ExecutionLogRecord],
    candidate_logs: list[ExecutionLogRecord],
    baseline_name: str = "baseline",
    candidate_name: str = "candidate",
) -> EvalReport:
    """比较两个版本的执行结果，生成评估报告。

    参数:
        baseline_logs: 基线版本的执行日志
        candidate_logs: 候选版本的执行日志
        baseline_name: 基线版本名
        candidate_name: 候选版本名

    返回:
        EvalReport 包含各项指标和判定
    """
    report = EvalReport(
        baseline_name=baseline_name,
        candidate_name=candidate_name,
        baseline_count=len(baseline_logs),
        candidate_count=len(candidate_logs),
    )

    if not baseline_logs or not candidate_logs:
        report.verdict = "neutral"
        report.details.append("Insufficient data for comparison")
        return report

    # 计算指标
    baseline_tokens = _avg_total_tokens(baseline_logs)
    candidate_tokens = _avg_total_tokens(candidate_logs)
    baseline_time = _avg_duration_ms(baseline_logs)
    candidate_time = _avg_duration_ms(candidate_logs)
    baseline_gate = _gate_pass_rate(baseline_logs)
    candidate_gate = _gate_pass_rate(candidate_logs)

    # Token efficiency: 正数表示候选版本更好（更少 token）
    if baseline_tokens > 0:
        report.token_efficiency = round(
            (baseline_tokens - candidate_tokens) / baseline_tokens, 4
        )
    else:
        report.token_efficiency = 0.0

    # Time efficiency: 正数表示候选版本更快
    if baseline_time > 0:
        report.time_efficiency = round(
            (baseline_time - candidate_time) / baseline_time, 4
        )
    else:
        report.time_efficiency = 0.0

    # Gate pass rate change
    report.gate_pass_rate_change = round(candidate_gate - baseline_gate, 4)

    # 生成细节
    details = []
    if report.token_efficiency > 0.05:
        details.append(f"Token usage reduced by {report.token_efficiency:.1%}")
    elif report.token_efficiency < -0.05:
        details.append(f"Token usage increased by {abs(report.token_efficiency):.1%}")

    if report.time_efficiency > 0.05:
        details.append(f"Execution time reduced by {report.time_efficiency:.1%}")
    elif report.time_efficiency < -0.05:
        details.append(f"Execution time increased by {abs(report.time_efficiency):.1%}")

    if report.gate_pass_rate_change > 0.01:
        details.append(f"Gate pass rate improved by {report.gate_pass_rate_change:.1%}")
    elif report.gate_pass_rate_change < -0.01:
        details.append(f"Gate pass rate decreased by {abs(report.gate_pass_rate_change):.1%}")

    if not details:
        # Check if everything is ~same
        if (abs(report.token_efficiency) < 0.05 and
            abs(report.time_efficiency) < 0.05 and
            abs(report.gate_pass_rate_change) < 0.01):
            details.append("No significant changes detected")
        else:
            details.append(f"Minor changes: token={report.token_efficiency:.3f}, time={report.time_efficiency:.3f}")

    report.details = details

    # 计算综合质量分
    report.quality_score = _compute_quality_score(
        report.token_efficiency,
        report.time_efficiency,
        report.gate_pass_rate_change,
    )

    # 判定
    if report.quality_score >= 0.55:
        report.verdict = "improved"
    elif report.quality_score <= 0.45:
        report.verdict = "regressed"
    else:
        report.verdict = "neutral"

    return report
