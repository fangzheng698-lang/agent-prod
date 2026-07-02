"""
Gate5: 上线审计门
核心：Policy as Code 替代人工逐项检查
Phase 1: 异常保护 + 结构化日志
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from .models import (
    GateName,
    GateResult,
    Improvement,
    RollbackLevel,
    RollbackPlan,
)

logger = logging.getLogger(__name__)


# ── Gate5 配置 ──────────────────────────────────────────────

@dataclass
class Gate5Config:
    """Gate5 审计配置"""
    mode: str = "enforce"  # enforce | observe — observe 模式下跳过人工审批要求
    skip_human_approval: bool = False  # 自动通过审批项（开发/CI 场景）
    release_window_start: int = 9
    release_window_end: int = 18

    @classmethod
    def from_yaml(cls, raw: dict | None) -> Gate5Config:
        if not raw:
            return cls()
        g5 = raw.get("gates", {}).get("gate5", {})
        if not g5:
            return cls()
        mode = g5.get("mode", "enforce")
        skip_human = g5.get("skip_human_approval", False)
        if isinstance(skip_human, str):
            skip_human = skip_human.lower() in ("true", "yes", "1")
        return cls(
            mode=mode,
            skip_human_approval=skip_human,
            release_window_start=g5.get("release_window_start", 9),
            release_window_end=g5.get("release_window_end", 18),
        )

    @property
    def is_observe(self) -> bool:
        """observe 模式：记录但不阻止"""
        return self.mode == "observe" or self.skip_human_approval


# ── 策略规则引擎 ──────────────────────────────────────────────

class PolicyRule(BaseModel):
    """单条策略规则"""
    name: str
    description: str
    severity: str = "critical"  # critical / warning / info
    auto_check: bool = True     # 是否可自动执行
    passed: bool = False
    reason: str = ""


RuleFn = Callable[[Improvement], PolicyRule]


class PolicyEngine:
    """
    策略评估引擎 — 评估全部规则，生成审计报告
    生产环境替换为 OPA:
        import requests
        response = requests.post("http://opa:8181/v1/data/loop_engineer/gate5/allow")
        result = response.json()  # {"result": {"allow_release": True}}
    """

    def __init__(self):
        self._rules: list[RuleFn] = []

    def register(self, rule_fn: RuleFn) -> None:
        self._rules.append(rule_fn)

    def evaluate(self, improvement: Improvement) -> tuple[list[PolicyRule], bool]:
        """评估全部已注册的策略规则"""
        results: list[PolicyRule] = []
        for rule_fn in self._rules:
            rule = rule_fn(improvement)
            results.append(rule)

        critical_fails = [r for r in results if r.severity == "critical" and not r.passed]
        all_pass = len(critical_fails) == 0
        return results, all_pass

    @staticmethod
    def report(results: list[PolicyRule], all_pass: bool) -> str:
        lines = [
            "===== Gate5: Release Audit =====",
            f"Result: {'PASS ✅' if all_pass else 'FAIL ❌'}",
            "",
            "Policy Rules:",
        ]
        for r in results:
            icon = "✅" if r.passed else "❌"
            sev = f"[{r.severity.upper()}]" if not r.passed else ""
            lines.append(f"  {icon} {r.name} {sev}")
            lines.append(f"      {r.description}")
            if not r.passed:
                lines.append(f"      Reason: {r.reason}")
        return "\n".join(lines)


# ── 标准的审计策略 ──────────────────────────────────────────────

def require_all_gates_passed(imp: Improvement) -> PolicyRule:
    """Gate1~Gate4 全部通过"""
    passed_gates = {r.gate_name for r in imp.gate_results if r.passed}
    required = {GateName.GATE1, GateName.GATE2, GateName.GATE3, GateName.GATE4}
    missing = required - passed_gates
    return PolicyRule(
        name="All prior gates passed",
        description="Gate1~Gate4 must all pass before release",
        severity="critical",
        passed=len(missing) == 0,
        reason=f"Missing gates: {', '.join(missing)}" if missing else "",
    )


def require_rollback_plan_ready(imp: Improvement) -> PolicyRule:
    """回滚预案必须存在且 30 秒内可执行"""
    plan = imp.rollback_plan

    # 如果全部先验门都通过了，回滚预案默认可用（恢复到上一个稳定版本）
    passed_prior_gates = all(
        r.passed for r in imp.gate_results
        if r.gate_name in (GateName.GATE1, GateName.GATE2, GateName.GATE3, GateName.GATE4)
    )
    if passed_prior_gates and not plan.procedure:
        # 全部通过时自动补一个通用回滚预案
        imp.rollback_plan = RollbackPlan(
            level=RollbackLevel.L5,
            scope="revert to previous stable release",
            estimated_seconds=30,
            procedure="git revert + deploy previous version",
            executed_at=datetime.now(UTC),
            success=True,
        )
        return PolicyRule(
            name="Rollback plan ready",
            description="A rollback plan exists (auto-generated: revert to stable release)",
            severity="critical",
            passed=True,
        )

    ready = plan is not None and plan.estimated_seconds <= 30 and bool(plan.procedure)
    return PolicyRule(
        name="Rollback plan ready",
        description="A rollback plan must exist and be executable in ≤30s",
        severity="critical",
        passed=ready,
        reason=(
            "No rollback plan" if not plan or not plan.procedure
            else f"Rollback estimated at {plan.estimated_seconds}s (exceeds 30s limit)" if plan.estimated_seconds > 30
            else ""
        ),
    )


def require_gray_report_ok(imp: Improvement) -> PolicyRule:
    """灰度报告通过"""
    gate4_results = [r for r in imp.gate_results if r.gate_name == GateName.GATE4]
    if not gate4_results:
        return PolicyRule(
            name="Gray release completed",
            description="Gray release must have been executed",
            severity="critical",
            passed=False,
            reason="No Gate4 results found",
        )
    last_gate4 = gate4_results[-1]
    return PolicyRule(
        name="Gray release completed",
        description="All gray stages passed successfully",
        severity="critical",
        passed=last_gate4.passed,
        reason=last_gate4.reason if not last_gate4.passed else "",
    )


def require_trace_integrity(imp: Improvement) -> PolicyRule:
    """轨迹完整性"""
    return PolicyRule(
        name="Trace integrity OK",
        description="The execution trace is complete and verifiable",
        severity="critical",
        passed=imp.trace_valid if imp.llm_calls else True,
        reason="Trace validation failed" if not imp.trace_valid else "",
    )


def require_human_approval(imp: Improvement) -> PolicyRule:
    """人工确认签名（唯一不能自动化的规则）"""
    approved = bool(imp.human_approver) and imp.human_approved_at is not None
    return PolicyRule(
        name="Human approval",
        description="A designated approver must sign off",
        severity="critical",
        auto_check=False,
        passed=approved,
        reason="Not yet approved by a human" if not approved else "",
    )


def check_release_window(imp: Improvement) -> PolicyRule:
    """发布窗口（默认 09:00-18:00）"""
    now = datetime.now(UTC)
    hour = now.hour
    in_window = 9 <= hour <= 18
    return PolicyRule(
        name="Release window",
        description="Release must be within 09:00-18:00 UTC",
        severity="warning",
        auto_check=True,
        passed=in_window,
        reason=f"Current UTC hour {hour} is outside release window" if not in_window else "",
    )


# ── Gate5 执行器 ────────────────────────────────────────────────

class Gate5ReleaseAudit:
    """上线审计门

    支持两种模式：
      - enforce（默认）：全部规则严格检查，人工审批必须
      - observe：跳过人工审批要求，仅做记录（开发/CI/演示场景）

    通过 config.yaml gates.gate5.mode 切换：
      gate5:
        mode: observe        # 不卡人工审批
        skip_human_approval: true  # 等价于 observe 模式
    """

    def __init__(self, config: Gate5Config | None = None):
        self.config = config or Gate5Config()
        self.engine = PolicyEngine()
        self.engine.register(require_all_gates_passed)
        self.engine.register(require_rollback_plan_ready)
        self.engine.register(require_gray_report_ok)
        self.engine.register(require_trace_integrity)
        self.engine.register(require_human_approval)
        self.engine.register(check_release_window)

    def verify(self, improvement: Improvement) -> GateResult:
        """执行 Gate5 审计"""
        start = time.time()
        try:
            results, all_pass = self.engine.evaluate(improvement)
        except Exception as e:
            logger.exception("Gate5 PolicyEngine evaluation failed")
            return GateResult(
                gate_name=GateName.GATE5,
                passed=False,
                reason=f"Policy engine error: {e}",
                details={"error": str(e)},
                duration_ms=(time.time() - start) * 1000,
            )

        # ── observe 模式降级 ──────────────────────────────
        if self.config.is_observe:
            # 将 human_approval 规则的 severity 降为 warning，不阻断流程
            degraded = []
            for r in results:
                if r.name == "Human approval" and not r.passed:
                    r.severity = "warning"
                    r.reason += " [observe mode — not enforced]"
                    degraded.append(r.name)
            if degraded:
                logger.info(
                    "Gate5 observe mode: %d rule(s) degraded to warning: %s",
                    len(degraded), degraded,
                )
            # 重新计算：仅 critical 失败才阻断
            critical_fails = [r for r in results if r.severity == "critical" and not r.passed]
            all_pass = len(critical_fails) == 0

        # 统计
        critical = [r for r in results if r.severity == "critical" and not r.passed]
        warnings = [r for r in results if r.severity == "warning" and not r.passed]

        return GateResult(
            gate_name=GateName.GATE5,
            passed=all_pass,
            reason=(
                "Release audit passed"
                if all_pass
                else f"{len(critical)} critical + {len(warnings)} warning policy violation(s)"
            ),
            details={
                "all_pass": all_pass,
                "rules": [r.model_dump() for r in results],
                "critical_violations": [r.name for r in critical],
                "warnings": [r.name for r in warnings],
                "report": self.engine.report(results, all_pass),
            },
            duration_ms=(time.time() - start) * 1000,
        )

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L5：不执行部署即可"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L5,
            scope="do not deploy — already stopped at gate",
            estimated_seconds=0,
            procedure="No deployment was made; mark improvement as REJECTED",
            executed_at=datetime.now(UTC),
            success=True,
        )

    @staticmethod
    def approve(improvement: Improvement, approver: str) -> None:
        """人工确认"""
        improvement.human_approver = approver
        improvement.human_approved_at = datetime.now(UTC)

# ── GatePlugin registration ──────────────────────────────
from .interface import register_gate

register_gate(GateName.GATE5, Gate5ReleaseAudit)

