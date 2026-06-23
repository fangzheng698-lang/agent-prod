"""
│Gate1: 执行验证门
│核心：用 Pydantic V2 的 response_format / structured output 契约替代事后 if 检查
│Phase 1: 阈值从 YAML 加载 + 异常保护
│"""
from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .models import (
    GateResult, GateName, Improvement, RollbackLevel, RollbackPlan,
)

logger = logging.getLogger(__name__)


# ── 执行输出契约 ──────────────────────────────────────────────
class ExecutionOutput(BaseModel, strict=True):
    """LLM 输出必须符合这个 Schema — 由 response_format 强制约束"""
    final_response: str = Field(min_length=1, max_length=100_000)
    confidence: float = Field(ge=0.0, le=1.0)
    tools_used: list[str] = Field(max_length=50)
    token_count: int = Field(gt=0, lt=1_000_000)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("final_response")
    @classmethod
    def response_not_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("final_response cannot be blank")
        return stripped


# ── 熔断器（Circuit Breaker） ──────────────────────────────────
class CircuitBreaker:
    """熔断器：连续失败超过阈值则熔断"""
    def __init__(self, threshold: int = 3, cooldown_seconds: float = 60.0):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.fail_count = 0
        self.last_fail_time = 0.0
        self.open = False

    def call(self, fn, *args, **kwargs) -> Any:
        if self.open:
            now = time.time()
            if now - self.last_fail_time > self.cooldown_seconds:
                self.open = False  # 半开
            else:
                raise RuntimeError(f"Circuit breaker OPEN (failed {self.fail_count}x)")

        try:
            result = fn(*args, **kwargs)
            self.fail_count = 0  # 成功则重置
            return result
        except Exception as e:
            self.fail_count += 1
            self.last_fail_time = time.time()
            if self.fail_count >= self.threshold:
                self.open = True
            raise


# ── Gate1 执行器 ────────────────────────────────────────────────
class Gate1Config(BaseModel):
    execution_time_tolerance: float = 1.2
    token_tolerance: float = 1.1
    consecutive_failures_before_escalation: int = 3
    circuit_breaker_cooldown_seconds: float = 60.0

    @classmethod
    def from_yaml(cls, data: dict | None) -> "Gate1Config":
        """从 config.yaml 的数据加载配置"""
        if not data:
            return cls()
        gate_cfg = data.get("gates", {}).get("gate1", {})
        return cls(**{k: v for k, v in gate_cfg.items()
                       if k in cls.model_fields})


class Gate1Execution:
    """执行验证门"""

    def __init__(self, config: Gate1Config | None = None):
        self.config = config or Gate1Config()
        self.circuit_breaker = CircuitBreaker(
            threshold=self.config.consecutive_failures_before_escalation,
            cooldown_seconds=self.config.circuit_breaker_cooldown_seconds,
        )

    @staticmethod
    def validate_structured_output(data: dict) -> ExecutionOutput:
        """
        用 Pydantic V2 校验结构化输出
        这是 Gate1 的核心——Schema 契约检查，不是事后 if
        """
        return ExecutionOutput.model_validate(data)

    def verify(self, improvement: Improvement) -> GateResult:
        """执行 Gate1 验证"""
        start = time.time()
        violations: list[str] = []

        # 1. Schema 契约校验
        try:
            output = ExecutionOutput.model_validate(improvement.candidate_output)
        except Exception as e:
            return GateResult(
                gate_name=GateName.GATE1,
                passed=False,
                reason=f"Output schema violation: {e}",
                details={"errors": str(e)},
                duration_ms=(time.time() - start) * 1000,
            )

        # 2. 预算校验
        if improvement.actual_time_ms > improvement.budget_time_ms * self.config.execution_time_tolerance:
            violations.append(
                f"Time over budget: {improvement.actual_time_ms}ms > "
                f"{improvement.budget_time_ms * self.config.execution_time_tolerance:.0f}ms"
            )

        if improvement.actual_tokens > improvement.budget_tokens * self.config.token_tolerance:
            violations.append(
                f"Tokens over budget: {improvement.actual_tokens} > "
                f"{improvement.budget_tokens * self.config.token_tolerance:.0f}"
            )

        # 3. Token 计数一致性
        if output.token_count != improvement.actual_tokens:
            violations.append(
                f"Token count mismatch: output claims {output.token_count}, "
                f"actual {improvement.actual_tokens}"
            )

        passed = len(violations) == 0
        return GateResult(
            gate_name=GateName.GATE1,
            passed=passed,
            reason="All checks passed" if passed else "; ".join(violations),
            details={
                "schema_valid": True,
                "violations": violations,
                "tokens": {"reported": output.token_count, "actual": improvement.actual_tokens},
                "time_ms": improvement.actual_time_ms,
            },
            duration_ms=(time.time() - start) * 1000,
        )

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L1 回滚：内存清理"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L1,
            scope="discard current TaskRun output",
            estimated_seconds=1,
            procedure="GC candidate_output — no persistent storage involved",
            executed_at=__import__("datetime").datetime.now(),
            success=True,
        )
        improvement.candidate_output = {}

# ── GatePlugin registration ──────────────────────────────
from .interface import register_gate
from .models import GateName
register_gate(GateName.GATE1, Gate1Execution)

