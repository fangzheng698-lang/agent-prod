"""Phase 6.3: Optimization Suggestion — 分析执行日志，生成改进建议。

检测: token 超支模式、重复 tool call、慢 gate、低门禁通过率。

用法:
    from agent_prod.testing.optimizer import analyze_logs, OptimizationSuggestion

    suggestions = analyze_logs(logs)
    for s in suggestions:
        print(f"[{s.severity}] {s.title}: {s.description}")
"""

from __future__ import annotations

from pydantic import BaseModel

from agent_prod.observability.execution_log import ExecutionLogRecord


class OptimizationSuggestion(BaseModel):
    """一条优化建议。"""
    category: str = "general"          # token_usage, time_efficiency, tool_calls, gate_quality
    severity: str = "info"             # info, warning, error, critical
    title: str = ""
    description: str = ""
    current_value: str = ""
    suggested_value: str = ""
    confidence: float = 0.5


# ── 阈值常量 ──
TOKEN_THRESHOLD_HIGH = 5000           # 平均 token 超过此值警告
TOKEN_THRESHOLD_CRITICAL = 20000      # 平均 token 超过此值严重
TIME_THRESHOLD_HIGH = 30000           # 平均耗时 30s 警告 (ms)
TIME_THRESHOLD_CRITICAL = 120000      # 平均耗时 120s 严重 (ms)
GATE_PASS_LOW = 0.5                   # 门禁通过率低于此值警告
GATE_PASS_CRITICAL = 0.2              # 门禁通过率低于此值严重
TURNS_THRESHOLD_HIGH = 5              # 平均轮次高于此值警告
SAMPLE_MIN = 3                        # 最小样本数才分析


def _avg_tokens_per_exec(logs: list[ExecutionLogRecord]) -> float:
    """平均 token 消耗。"""
    if not logs:
        return 0.0
    total = sum(
        r.costs.get("prompt_tokens", 0) + r.costs.get("completion_tokens", 0)
        for r in logs
    )
    return total / len(logs)


def _avg_duration_ms(logs: list[ExecutionLogRecord]) -> float:
    if not logs:
        return 0.0
    return sum(r.duration_ms for r in logs) / len(logs)


def _gate_pass_rate(logs: list[ExecutionLogRecord]) -> float:
    if not logs:
        return 0.0
    passed = sum(1 for r in logs if r.quality_gate_result.get("passed", False))
    return passed / len(logs)


def _avg_turns(logs: list[ExecutionLogRecord]) -> float:
    if not logs:
        return 0.0
    return sum(r.turns for r in logs) / len(logs)


def _most_common_gate_failure(logs: list[ExecutionLogRecord]) -> str | None:
    """找出最常见的门禁失败原因。"""
    failures: dict[str, int] = {}
    for r in logs:
        qg = r.quality_gate_result
        if not qg.get("passed", False) and qg.get("failed_at"):
            gate = qg["failed_at"]
            failures[gate] = failures.get(gate, 0) + 1
    if not failures:
        return None
    return max(failures, key=failures.get)


def analyze_logs(logs: list[ExecutionLogRecord]) -> list[OptimizationSuggestion]:
    """分析执行日志并生成优化建议列表。

    参数:
        logs: ExecutionLogRecord 列表

    返回:
        OptimizationSuggestion 列表，按严重程度排序
    """
    suggestions: list[OptimizationSuggestion] = []

    if len(logs) < SAMPLE_MIN:
        return suggestions

    _n = len(logs)
    avg_tokens = _avg_tokens_per_exec(logs)
    avg_time = _avg_duration_ms(logs)
    gate_rate = _gate_pass_rate(logs)
    avg_t = _avg_turns(logs)

    # ── 1. Token 超支检测 ──
    if avg_tokens > TOKEN_THRESHOLD_CRITICAL:
        suggestions.append(OptimizationSuggestion(
            category="token_usage",
            severity="critical",
            title="Severe token overrun detected",
            description=(
                f"Average tokens per execution is {avg_tokens:.0f}, "
                f"far exceeding critical threshold of {TOKEN_THRESHOLD_CRITICAL}. "
                f"Consider reducing prompt size, limiting tool calls, or adding stricter budget controls."
            ),
            current_value=str(int(avg_tokens)),
            suggested_value=f"< {TOKEN_THRESHOLD_HIGH}",
            confidence=min(0.95, avg_tokens / TOKEN_THRESHOLD_CRITICAL * 0.8),
        ))
    elif avg_tokens > TOKEN_THRESHOLD_HIGH:
        confidence_val = min(0.85, avg_tokens / TOKEN_THRESHOLD_HIGH * 0.6)
        suggestions.append(OptimizationSuggestion(
            category="token_usage",
            severity="warning",
            title="Elevated token usage",
            description=(
                f"Average tokens per execution is {avg_tokens:.0f}, "
                f"exceeding recommended threshold of {TOKEN_THRESHOLD_HIGH}. "
                f"Review prompt efficiency and tool call patterns."
            ),
            current_value=str(int(avg_tokens)),
            suggested_value=f"< {TOKEN_THRESHOLD_HIGH}",
            confidence=confidence_val,
        ))

    # ── 2. 时间效率检测 ──
    if avg_time > TIME_THRESHOLD_CRITICAL:
        suggestions.append(OptimizationSuggestion(
            category="time_efficiency",
            severity="critical",
            title="Severe execution latency",
            description=(
                f"Average execution time is {avg_time/1000:.0f}s, "
                f"exceeding critical threshold of {TIME_THRESHOLD_CRITICAL/1000:.0f}s. "
                f"Check for LLM timeout issues, inefficient tool calls, or network delays."
            ),
            current_value=f"{avg_time/1000:.1f}s",
            suggested_value=f"< {TIME_THRESHOLD_HIGH/1000:.0f}s",
            confidence=min(0.95, avg_time / TIME_THRESHOLD_CRITICAL * 0.8),
        ))
    elif avg_time > TIME_THRESHOLD_HIGH:
        confidence_val = min(0.85, avg_time / TIME_THRESHOLD_HIGH * 0.6)
        suggestions.append(OptimizationSuggestion(
            category="time_efficiency",
            severity="warning",
            title="Slow execution times",
            description=(
                f"Average execution time is {avg_time/1000:.1f}s, "
                f"above recommended threshold of {TIME_THRESHOLD_HIGH/1000:.0f}s."
            ),
            current_value=f"{avg_time/1000:.1f}s",
            suggested_value=f"< {TIME_THRESHOLD_HIGH/1000:.0f}s",
            confidence=confidence_val,
        ))

    # ── 3. 门禁通过率检测 ──
    if gate_rate <= GATE_PASS_CRITICAL:
        most_common = _most_common_gate_failure(logs)
        desc = (
            f"Gate pass rate is critically low at {gate_rate:.1%}. "
        )
        if most_common:
            desc += f"Most common failure: {most_common}. "
        desc += "Investigate gate thresholds and execution quality."
        suggestions.append(OptimizationSuggestion(
            category="gate_quality",
            severity="critical",
            title="Critically low gate pass rate",
            description=desc,
            current_value=f"{gate_rate:.1%}",
            suggested_value=f"> {GATE_PASS_LOW:.0%}",
            confidence=min(0.95, (1.0 - gate_rate) * 1.5),
        ))
    elif gate_rate <= GATE_PASS_LOW:
        most_common = _most_common_gate_failure(logs)
        desc = f"Gate pass rate is low at {gate_rate:.1%}. "
        if most_common:
            desc += f"Most common failure: {most_common}. "
        desc += "Consider reviewing gate configurations."
        suggestions.append(OptimizationSuggestion(
            category="gate_quality",
            severity="warning",
            title="Low gate pass rate",
            description=desc,
            current_value=f"{gate_rate:.1%}",
            suggested_value=f"> {GATE_PASS_LOW:.0%}",
            confidence=min(0.85, (GATE_PASS_LOW - gate_rate) * 2.0 + 0.3),
        ))

    # ── 4. 高轮次检测（可能工具调用过多） ──
    if avg_t > TURNS_THRESHOLD_HIGH:
        suggestions.append(OptimizationSuggestion(
            category="tool_calls",
            severity="warning",
            title="High number of turns per execution",
            description=(
                f"Average {avg_t:.1f} turns per execution, "
                f"indicating potentially excessive tool calls or LLM loops. "
                f"Consider improving tool efficiency or adding early termination conditions."
            ),
            current_value=f"{avg_t:.1f} turns",
            suggested_value=f"< {TURNS_THRESHOLD_HIGH} turns",
            confidence=min(0.8, (avg_t - TURNS_THRESHOLD_HIGH) / TURNS_THRESHOLD_HIGH * 0.7),
        ))

    # ── 5. 门禁失败模式分析 ──
    failed_at = _most_common_gate_failure(logs)
    if failed_at and gate_rate < 0.7:
        suggestions.append(OptimizationSuggestion(
            category="gate_quality",
            severity="info" if gate_rate > GATE_PASS_LOW else "warning",
            title=f"Frequent failures at {failed_at}",
            description=(
                f"The most failed gate is '{failed_at}'. "
                f"Review gate configuration and execution patterns that trigger this failure."
            ),
            current_value=failed_at,
            suggested_value="Review gate threshold",
            confidence=min(0.75, (1.0 - gate_rate)),
        ))

    # 按严重程度排序: critical > error > warning > info
    severity_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    suggestions.sort(key=lambda s: severity_order.get(s.severity, 5))

    return suggestions
