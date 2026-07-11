# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""质量门共享数据模型"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# 延迟导入引用（在 Improvement 使用前设置）
_reasoning_chain_cls = None
_provenance_cls = None


def _get_reasoning_chain():
    global _reasoning_chain_cls
    if _reasoning_chain_cls is None:
        from .reasoning import ReasoningChain as _reasoning_chain_cls
    return _reasoning_chain_cls


def _get_provenance():
    global _provenance_cls
    if _provenance_cls is None:
        from .provenance import DataProvenance as _provenance_cls
    return _provenance_cls


class ImprovementStatus(str, Enum):
    """改进项生命周期状态"""
    CANDIDATE = "candidate"          # 待验证
    GATE1_PASSED = "gate1_passed"    # 执行验证通过
    GATE2_PASSED = "gate2_passed"    # 轨迹完整性通过
    GATE3_PASSED = "gate3_passed"    # 回归验证通过
    GATE4_PASSED = "gate4_passed"    # 灰度放行通过
    GATE5_PASSED = "gate5_passed"    # 审批通过
    GATE6_PASSED = "gate6_passed"    # 答案质量验证通过
    GATE7_PASSED = "gate7_passed"    # 执行一致性验证通过
    PRODUCTION = "production"        # 正式上线
    REJECTED = "rejected"            # 被拒绝
    ROLLED_BACK = "rolled_back"      # 已回滚

class GateName(str, Enum):
    """质量门名称"""
    GATE0 = "gate0_permission"
    GATE1 = "gate1_execution"
    GATE2 = "gate2_trace_integrity"
    GATE3 = "gate3_regression"
    GATE4 = "gate4_gray_release"
    GATE5 = "gate5_release_audit"
    GATE6 = "gate6_answer_quality"
    GATE7 = "gate7_execution_consistency"


class RollbackLevel(int, Enum):
    L1 = 1   # 内存清理
    L2 = 2   # 数据库回滚
    L3 = 3   # Benchmark 快照
    L4 = 4   # 流量切回
    L5 = 5   # 不上线即可


class GateResult(BaseModel):
    """单道质量门的执行结果"""
    gate_name: GateName
    passed: bool
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_summary(self) -> str:
        status = "✅ PASS" if self.passed else "❌ FAIL"
        return f"[{status}] {self.gate_name} ({self.duration_ms:.0f}ms) | {self.reason}"


class RollbackPlan(BaseModel):
    """回滚预案"""
    level: RollbackLevel = RollbackLevel.L1
    scope: str = ""
    estimated_seconds: int = 5
    procedure: str = ""
    executed_at: datetime | None = None
    success: bool = False


class Improvement(BaseModel):
    """一个被质量门评估的改进提案"""
    id: str = Field(default_factory=lambda: f"imp-{uuid.uuid4().hex[:8]}")
    name: str
    status: ImprovementStatus = ImprovementStatus.CANDIDATE

    # 持久化追踪
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    # 输入数据
    baseline_output: dict[str, Any] = Field(default_factory=dict)
    candidate_output: dict[str, Any] = Field(default_factory=dict)

    # 预算
    budget_tokens: int = 100_000
    budget_time_ms: int = 60_000
    actual_tokens: int = 0
    actual_time_ms: int = 0

    # 追踪
    trace_id: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)

    # 灰度
    traffic_percentage: int = 0
    gray_stage: int = 0  # 0=未灰度, 1=1%, 2=10%, 3=50%, 4=100%

    # 审计
    gate_results: list[GateResult] = Field(default_factory=list)
    fail_gate: str = ""
    fail_reason: str = ""
    rollback_plan: RollbackPlan = Field(default_factory=RollbackPlan)
    human_approver: str = ""
    human_approved_at: datetime | None = None

    # 推理链与数据溯源（Phase 1: 行业升级）
    reasoning_chain: Any = None  # ReasoningChain, 延迟导入避免循环
    data_provenance: Any = None  # DataProvenance, 延迟导入避免循环

    # Pydantic V2 序列化配置
    model_config = {"extra": "ignore", "ser_json_timedelta": "iso8601"}

    def mark_updated(self) -> None:
        self.updated_at = datetime.now(UTC)

    def init_reasoning_chain(self) -> None:
        """惰性初始化推理链和数据溯源"""
        if self.reasoning_chain is None:
            RC = _get_reasoning_chain()
            self.reasoning_chain = RC(improvement_id=self.id)
        if self.data_provenance is None:
            DP = _get_provenance()
            self.data_provenance = DP(improvement_id=self.id)

    def add_result(self, result: GateResult) -> None:
        self.gate_results.append(result)
        if result.passed:
            status_map = {
                GateName.GATE1: ImprovementStatus.GATE1_PASSED,
                GateName.GATE2: ImprovementStatus.GATE2_PASSED,
                GateName.GATE3: ImprovementStatus.GATE3_PASSED,
                GateName.GATE4: ImprovementStatus.GATE4_PASSED,
                GateName.GATE6: ImprovementStatus.GATE6_PASSED,
            }
            if result.gate_name in status_map:
                self.status = status_map[result.gate_name]
            if result.gate_name == GateName.GATE6 and result.passed:
                self.status = ImprovementStatus.PRODUCTION
        else:
            self.fail_gate = result.gate_name
            self.fail_reason = result.reason

    @property
    def trace_valid(self) -> bool:
        """轨迹完整性：所有 llm_calls 和 tool_calls 是否有对应关系"""
        if not self.llm_calls:
            return False
        if not self.tool_calls:
            return True  # 没有工具调用也允许
        # 每个工具调用应当由某次 LLM 调用产生
        tool_callers = {tc.get("request_id") for tc in self.tool_calls}
        llm_ids = {lc.get("response_id") for lc in self.llm_calls}
        return tool_callers.issubset(llm_ids) if tool_callers else True

    def to_report(self) -> str:
        lines = [
            f"=== Improvement Report: {self.name} ===",
            f"  ID:     {self.id}",
            f"  Status: {self.status.value}",
            f"  Budget: {self.actual_tokens}/{self.budget_tokens} tokens, {self.actual_time_ms}/{self.budget_time_ms}ms",
            f"  Trace:  {'✅ valid' if self.trace_valid else '❌ invalid'} [{len(self.llm_calls)} LLM calls, {len(self.tool_calls)} tool calls]",
            "",
            "  Quality Gates:",
        ]
        for gr in self.gate_results:
            lines.append(f"    {gr.to_summary()}")
        if self.fail_gate:
            lines.append(f"  FAILED AT: {self.fail_gate} — {self.fail_reason}")
        lines.append("")
        return "\n".join(lines)
