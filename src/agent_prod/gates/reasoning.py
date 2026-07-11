# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""推理链模型 — 记录每个 Gate 的决策过程与证据来源。

每个 Gate 在执行 verify() 时，将决策理由、证据来源、置信度记录到
Improvement.reasoning_chain 中。最终可通过 to_report() 输出完整的
可追溯决策报告，满足金融/能源行业的监管审计要求。

设计决策:
  - ReasoningChain 是 append-only，追加后不可修改
  - ReasoningStep 携带 EvidenceSource 列表，而非单一证据
  - 置信度 0.0-1.0，允许下游按权重聚合
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvidenceType(str, Enum):
    """证据来源类型"""
    METRIC = "metric"                # 数值指标（延迟、成功率等）
    POLICY_RULE = "policy_rule"      # 策略规则（权限、审计规则）
    LLM_JUDGMENT = "llm_judgment"    # LLM 判断（Gate6 checklist）
    PATTERN_MATCH = "pattern_match"  # 模式匹配（正则、意图分类）
    HUMAN_INPUT = "human_input"      # 人工输入（审批人决定）
    STATISTICAL = "statistical"      # 统计计算（EWMA、t-test）
    COMPARISON = "comparison"        # 对比分析（diff、回归检测）
    STRUCTURAL = "structural"        # 结构校验（schema、DAG）


class EvidenceSource(BaseModel):
    """一条证据来源"""
    type: EvidenceType
    name: str = ""
    value: Any = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    model_config = {"extra": "ignore"}


class ReasoningStep(BaseModel):
    """一次决策步骤"""
    step_id: str
    gate: str                          # 哪个 Gate
    decision: str                      # 决策结果（PASS/FAIL/BLOCK/ALLOW/FLAG）
    reason: str = ""                   # 人类可读的理由
    evidence: list[EvidenceSource] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    model_config = {"extra": "ignore"}

    def to_line(self, indent: str = "  ") -> str:
        ev_lines = "\n".join(
            f'{indent}  ├─ [{e.type.value}] {e.name}: {e.value}'
            for e in self.evidence
        )
        return (
            f"{indent}[{self.gate}] {self.decision} (conf={self.confidence:.2f})\n"
            f"{indent}  └─ {self.reason}\n"
            f"{ev_lines}"
        )


class ReasoningChain(BaseModel):
    """完整的推理链，由多个 ReasoningStep 组成"""
    improvement_id: str = ""
    steps: list[ReasoningStep] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    def add_step(self, step: ReasoningStep) -> None:
        self.steps.append(step)

    def get_steps_by_gate(self, gate: str) -> list[ReasoningStep]:
        return [s for s in self.steps if s.gate == gate]

    def to_report(self) -> str:
        if not self.steps:
            return f"ReasoningChain[{self.improvement_id}]: no steps recorded"
        lines = [
            f"ReasoningChain: {self.improvement_id}",
            f"  Total steps: {len(self.steps)}",
            "",
        ]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"Step {i}:")
            lines.append(step.to_line())
            lines.append("")
        return "\n".join(lines)