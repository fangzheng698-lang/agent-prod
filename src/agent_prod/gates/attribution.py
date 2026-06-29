"""Gate3 归因引擎 — 从指标级回归定位到 decision/tool_call 级根因。

当 Gate3 检测到回归（latency_p95 上升、success_rate 下降等），
AttributionEngine 对比 baseline 和 candidate 的 decisions/tool_calls，
逐层定位根因：

    metric 回归 → decision 级归因 → tool_call 级归因 → 参数级归因

输出 AttribReport：可读的根因报告 + fix_prompt（供 B2 自动修复管道使用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallDiff:
    """单个 tool_call 的归因分析."""
    tool_name: str
    tool_id: str = ""
    baseline_args: dict[str, Any] | None = None
    candidate_args: dict[str, Any] | None = None
    baseline_success: bool = True
    candidate_success: bool = True
    baseline_duration_ms: float = 0.0
    candidate_duration_ms: float = 0.0
    delta_duration_ms: float = 0.0
    arg_diffs: list[str] = field(default_factory=list)
    contribution_pct: float = 0.0  # 对该决策的贡献百分比


@dataclass
class DecisionDiff:
    """单个 decision 的归因分析."""
    decision_id: str
    model: str = ""
    baseline_prompt_tokens: int = 0
    candidate_prompt_tokens: int = 0
    baseline_completion_tokens: int = 0
    candidate_completion_tokens: int = 0
    tool_call_diffs: list[ToolCallDiff] = field(default_factory=list)
    tool_calls_added: int = 0
    tool_calls_removed: int = 0
    contribution_pct: float = 0.0


@dataclass
class AttributionReport:
    """完整的根因归因报告."""
    attribution_id: str = ""
    passed: bool = True

    # 指标级
    field: str = ""                    # 触发归因的指标字段
    baseline_value: Any = None
    candidate_value: Any = None
    delta_pct: float = 0.0

    # decision 级
    decision_diffs: list[DecisionDiff] = field(default_factory=list)
    decisions_added: int = 0
    decisions_removed: int = 0

    # 根因摘要
    root_cause: str = ""              # 人类可读根因
    fix_hint: str = ""                # 修复方向建议
    fix_prompt: str = ""              # 可直接喂给 agent 的修复 prompt
    severity: str = "info"            # info / warning / critical


class AttributionEngine:
    """归因引擎 — 从指标回归定位到 tool_call 级根因.

    Usage:
        engine = AttributionEngine()
        report = engine.attribute(
            field="latency_p95_ms",
            baseline_value=300.0,
            candidate_value=800.0,
            baseline_decisions=[...],   # 上次 PRODUCTION 的 decisions
            candidate_decisions=[...],  # 本次候选的 decisions
        )
        print(report.root_cause)   # "决策 d2 中 search 工具调用耗时从 120ms 飙升至 650ms"
        print(report.fix_prompt)   # 可喂给 agent 的修复指令
    """

    @staticmethod
    def attribute(
        field: str,
        baseline_value: Any,
        candidate_value: Any,
        baseline_decisions: list[dict] | None = None,
        candidate_decisions: list[dict] | None = None,
        attribution_id: str = "",
    ) -> AttributionReport:
        """执行归因分析."""
        baseline_decisions = baseline_decisions or []
        candidate_decisions = candidate_decisions or []

        delta_pct = _compute_delta_pct(baseline_value, candidate_value)

        report = AttributionReport(
            attribution_id=attribution_id,
            field=field,
            baseline_value=baseline_value,
            candidate_value=candidate_value,
            delta_pct=delta_pct,
        )

        # 1) decision 级对比
        baseline_ids = {d.get("decision_id", f"b{i}"): d for i, d in enumerate(baseline_decisions)}
        candidate_ids = {d.get("decision_id", f"c{i}"): d for i, d in enumerate(candidate_decisions)}

        all_ids = set(baseline_ids.keys()) | set(candidate_ids.keys())

        # 新增/删除
        report.decisions_added = len(candidate_ids - baseline_ids)
        report.decisions_removed = len(baseline_ids - candidate_ids)

        total_delta = 0.0
        for did in all_ids:
            bd = baseline_ids.get(did)
            cd = candidate_ids.get(did)
            dd = _compare_decisions(did, bd, cd)
            report.decision_diffs.append(dd)
            total_delta += abs(dd.contribution_pct)

        # 归一化贡献百分比，并累加 tool_call 级 delta
        if report.decision_diffs and total_delta > 0:
            for dd in report.decision_diffs:
                dd.contribution_pct = round(dd.contribution_pct / total_delta * 100, 1)

        # 2) 生成根因摘要
        report.root_cause, report.fix_hint = _build_root_cause(report)

        # 3) 生成修复 prompt
        report.fix_prompt = _build_fix_prompt(report)

        # 4) 判定 severity
        if delta_pct > 20:
            report.severity = "critical"
        elif delta_pct > 5:
            report.severity = "warning"
        else:
            report.severity = "info"

        report.passed = report.severity != "critical"
        return report

    @staticmethod
    def attribute_metric_degradation(
        metric_name: str,
        baseline_dict: dict,
        candidate_dict: dict,
        baseline_decisions: list[dict] | None = None,
        candidate_decisions: list[dict] | None = None,
    ) -> AttributionReport | None:
        """便捷方法：自动从 baseline/candidate dict 归因."""
        bv = baseline_dict.get(metric_name)
        cv = candidate_dict.get(metric_name)
        if bv is None or cv is None:
            return None
        if not _is_degradation(bv, cv):
            return None
        return AttributionEngine.attribute(
            field=metric_name,
            baseline_value=bv,
            candidate_value=cv,
            baseline_decisions=baseline_decisions,
            candidate_decisions=candidate_decisions,
        )


# ── helpers ──────────────────────────────────────────────────

def _compute_delta_pct(baseline: Any, candidate: Any) -> float:
    try:
        bv, cv = float(baseline), float(candidate)
        if bv == 0:
            return 100.0 if cv != 0 else 0.0
        return round((cv - bv) / abs(bv) * 100, 1)
    except (TypeError, ValueError):
        return 0.0


def _is_degradation(baseline: Any, candidate: Any) -> bool:
    try:
        bv, cv = float(baseline), float(candidate)
        return cv < bv  # 指标下降
    except (TypeError, ValueError):
        return False


def _compare_decisions(
    decision_id: str,
    baseline: dict | None,
    candidate: dict | None,
) -> DecisionDiff:
    dd = DecisionDiff(decision_id=decision_id)

    if baseline is None and candidate is None:
        return dd
    if baseline is None:
        dd.contribution_pct = 100.0
        return dd
    if candidate is None:
        dd.contribution_pct = 100.0
        return dd

    # 模型
    dd.model = candidate.get("model", baseline.get("model", ""))

    # Token 变化
    dd.baseline_prompt_tokens = baseline.get("prompt_tokens", 0)
    dd.candidate_prompt_tokens = candidate.get("prompt_tokens", 0)
    dd.baseline_completion_tokens = baseline.get("completion_tokens", 0)
    dd.candidate_completion_tokens = candidate.get("completion_tokens", 0)

    # tool_call 级对比
    b_tools = {tc.get("tool_id", f"bt{i}"): tc for i, tc in enumerate(baseline.get("tool_calls", []))}
    c_tools = {tc.get("tool_id", f"ct{i}"): tc for i, tc in enumerate(candidate.get("tool_calls", []))}

    dd.tool_calls_added = len(set(c_tools.keys()) - set(b_tools.keys()))
    dd.tool_calls_removed = len(set(b_tools.keys()) - set(c_tools.keys()))

    for tid in set(b_tools.keys()) | set(c_tools.keys()):
        bt = b_tools.get(tid)
        ct = c_tools.get(tid)
        td = _compare_tool_calls(tid, bt, ct)
        dd.tool_call_diffs.append(td)
        dd.contribution_pct += abs(td.delta_duration_ms)

    return dd


def _compare_tool_calls(
    tool_id: str,
    baseline: dict | None,
    candidate: dict | None,
) -> ToolCallDiff:
    td = ToolCallDiff(tool_name="", tool_id=tool_id)

    if baseline is None and candidate is None:
        return td
    if baseline is None:
        td.tool_name = candidate.get("tool_name", "unknown") if candidate else "unknown"
        td.contribution_pct = 100.0
        return td
    if candidate is None:
        td.tool_name = baseline.get("tool_name", "unknown")
        td.contribution_pct = 100.0
        return td

    td.tool_name = candidate.get("tool_name", baseline.get("tool_name", "unknown"))

    # 参数 diff
    b_args = baseline.get("arguments", {}) or {}
    c_args = candidate.get("arguments", {}) or {}
    for key in set(b_args.keys()) | set(c_args.keys()):
        if b_args.get(key) != c_args.get(key):
            td.arg_diffs.append(
                f"{key}: {b_args.get(key)} → {c_args.get(key)}"
            )

    # 耗时变化
    td.baseline_duration_ms = baseline.get("duration_ms", 0.0)
    td.candidate_duration_ms = candidate.get("duration_ms", 0.0)
    td.delta_duration_ms = td.candidate_duration_ms - td.baseline_duration_ms

    # 成功/失败
    td.baseline_success = baseline.get("success", True)
    td.candidate_success = candidate.get("success", True)
    td.contribution_pct = abs(td.delta_duration_ms)

    return td


def _build_root_cause(report: AttributionReport) -> tuple[str, str]:
    """生成根因摘要和修复方向."""
    parts = []
    hints = []

    # 新增/删除 decisions
    if report.decisions_added:
        parts.append(f"{report.decisions_added} 个新增决策")
        hints.append(f"检查是否必要新增 {report.decisions_added} 个决策")
    if report.decisions_removed:
        parts.append(f"{report.decisions_removed} 个决策被移除")
        hints.append(f"检查是否误删 {report.decisions_removed} 个决策")

    # 耗时 top contributors
    slow_tools = []
    for dd in report.decision_diffs:
        for td in dd.tool_call_diffs:
            if abs(td.delta_duration_ms) > 50:
                slow_tools.append(
                    f"d[{dd.decision_id[:8]}] {td.tool_name}: "
                    f"{td.baseline_duration_ms:.0f}ms → {td.candidate_duration_ms:.0f}ms "
                    f"({'↑' if td.delta_duration_ms > 0 else '↓'}{abs(td.delta_duration_ms):.0f}ms)"
                )

    if slow_tools:
        top = slow_tools[:3]
        parts.append("工具耗时异常: " + "; ".join(top))
        hints.append("优先检查耗时最高的工具调用")

    # 参数变化
    arg_changes = []
    for dd in report.decision_diffs:
        for td in dd.tool_call_diffs:
            if td.arg_diffs:
                arg_changes.append(f"{td.tool_name}: {', '.join(td.arg_diffs[:2])}")

    if arg_changes:
        parts.append("参数变化: " + "; ".join(arg_changes[:3]))
        hints.append("对比参数差异，确认是否为预期行为")

    # Token 异常
    token_deltas = []
    for dd in report.decision_diffs:
        token_delta = (dd.candidate_prompt_tokens + dd.candidate_completion_tokens) - (
            dd.baseline_prompt_tokens + dd.baseline_completion_tokens
        )
        if abs(token_delta) > 200:
            token_deltas.append(f"d[{dd.decision_id[:8]}] token Δ={token_delta:+d}")

    if token_deltas:
        parts.append("Token 消耗异常: " + "; ".join(token_deltas[:3]))
        hints.append("检查 prompt/tool 结果是否膨胀")

    root_cause = "; ".join(parts) if parts else "未检测到明显根因"
    fix_hint = "; ".join(hints) if hints else "进一步分析 decisions 差异"

    return root_cause, fix_hint


def _build_fix_prompt(report: AttributionReport) -> str:
    """生成可直接喂给 agent 的修复 prompt."""
    lines = [
        f"## 回归根因分析\n",
        f"触发指标: {report.field}",
        f"基线值: {report.baseline_value} → 候选值: {report.candidate_value} "
        f"(变化: {report.delta_pct:+.1f}%)\n",
        f"### 根因\n{report.root_cause}\n",
        f"### 修复建议\n{report.fix_hint}\n",
    ]

    # 附每个异常 decision 详情
    for i, dd in enumerate(report.decision_diffs, 1):
        issues = []
        if dd.tool_calls_added:
            issues.append(f"+{dd.tool_calls_added} 个工具调用新增")
        if dd.tool_calls_removed:
            issues.append(f"-{dd.tool_calls_removed} 个工具调用移除")
        if abs(dd.contribution_pct) > 5.0:
            issues.append(f"贡献度 {dd.contribution_pct:.1f}%")

        if issues:
            lines.append(
                f"### 决策 {dd.decision_id[:8]} ({', '.join(issues)})\n"
            )
            for td in dd.tool_call_diffs:
                if abs(td.delta_duration_ms) > 20 or td.arg_diffs:
                    lines.append(
                        f"- `{td.tool_name}`: {td.baseline_duration_ms:.0f}ms → "
                        f"{td.candidate_duration_ms:.0f}ms  |  "
                        f"args: {', '.join(td.arg_diffs) if td.arg_diffs else '无变化'}\n"
                    )

    lines.append("\n## 修复指令\n")
    lines.append(
        "请根据以上根因分析调整 agent 配置：\n"
        "1. 检查参数异常的 tool_call，确认是否为错误参数\n"
        "2. 检查耗时飙升的工具调用，考虑优化策略\n"
        "3. 确认新增/删除的决策是否为预期行为\n"
    )

    return "\n".join(lines)
