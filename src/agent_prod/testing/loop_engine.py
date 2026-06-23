"""Phase 7.1: LoopEngine — 三层闭环架构。

Execution → Optimization → Release 三层连成闭环:

    ┌──────────────────────────────────────────┐
    │  LoopEngine.run_loop(prompt, iter)       │
    │    │                                      │
    │    ├─ 1. Execution Layer: run_task()     │
    │    ├─ 2. Optimization Layer: optimize()  │
    │    └─ 3. Release Layer: release_check()  │
    └──────────────────────────────────────────┘

用法:
    le = LoopEngine(name="my-loop")
    result = le.run_task(prompt="hi", context="", response_fn=...)
    report = le.optimize(results, optimize_fn)
    decision = le.release_check(gate_pass_rate=0.95, error_rate=0.01)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ExecutionResult:
    """执行层输出：单次任务执行结果。"""
    run_id: str
    session_id: str
    response: str
    turns: int
    tokens_used: int
    time_ms: int
    gate_pass: bool
    gate_status: str
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_per_token(self) -> float:
        return round(self.time_ms / max(1, self.tokens_used), 4)

    def is_success(self) -> bool:
        return self.gate_pass and not self.error


@dataclass
class OptimizationReport:
    """优化层输出：在一次运行循环中生成的改进。"""
    summary: str
    suggestions: list[str] = field(default_factory=list)
    score_improvement: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReleaseDecision:
    """发布层输出：发布决策。"""
    action: str  # "promote", "reject", "hold", "rollback"
    reason: str
    version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class LoopEngine:
    """三层闭环引擎 — Execution → Optimization → Release 的顶层编排。

    使用回调模式解耦各层：
        - response_fn: 模拟/转发 LLM 调用
        - optimize_fn: 模拟/实现优化策略
    """

    def __init__(self, name: str):
        self.name = name
        self.execution_count = 0
        self._history: list[ExecutionResult] = []

    # ── Layer 1: Execution ──

    def run_task(
        self,
        prompt: str,
        context: str,
        response_fn: Callable[[str, str], ExecutionResult],
    ) -> ExecutionResult:
        """运行单次任务，调用回调获取 ExecutionResult。"""
        result = response_fn(prompt, context)
        self.execution_count += 1
        self._history.append(result)
        return result

    # ── Layer 2: Optimization ──

    def optimize(
        self,
        results: list[ExecutionResult],
        optimize_fn: Callable[[list[ExecutionResult]], OptimizationReport],
    ) -> OptimizationReport:
        """分析结果并生成优化建议。"""
        return optimize_fn(results)

    # ── Layer 3: Release ──

    def release_check(
        self,
        gate_pass_rate: float,
        error_rate: float,
        *,
        min_pass_rate: float = 0.9,
        max_error_rate: float = 0.05,
    ) -> ReleaseDecision:
        """基于门禁通过率和错误率做出发布决策。"""
        if error_rate > max_error_rate:
            return ReleaseDecision(
                action="reject",
                reason=f"Error rate {error_rate:.1%} exceeds max {max_error_rate:.1%}",
            )
        if gate_pass_rate < min_pass_rate:
            return ReleaseDecision(
                action="reject",
                reason=f"Gate pass rate {gate_pass_rate:.1%} below min {min_pass_rate:.1%}",
            )
        return ReleaseDecision(
            action="promote",
            reason=f"Gate pass rate {gate_pass_rate:.1%} ≥ {min_pass_rate:.1%}, "
                   f"error rate {error_rate:.1%} ≤ {max_error_rate:.1%}",
        )

    # ── Full Loop ──

    def run_loop(
        self,
        prompt: str,
        context: str = "",
        *,
        iterations: int = 3,
        response_fn: Callable | None = None,
        optimize_fn: Callable | None = None,
    ) -> list[dict[str, Any]]:
        """运行完整闭环：执行 → 检查 → 优化 → 再执行。"""
        if response_fn is None:
            raise ValueError("response_fn is required")
        if optimize_fn is None:
            def _noop(results):
                return OptimizationReport(summary="No optimization applied")
            optimize_fn = _noop

        loop_results = []
        for i in range(iterations):
            result = self.run_task(f"{prompt} (iter {i+1})", context, response_fn)
            loop_results.append({
                "iteration": i + 1,
                "result": result,
            })

            if not result.gate_pass:
                if result.error:
                    loop_results[-1]["status"] = "error"
                else:
                    loop_results[-1]["status"] = "gates_failed"
                continue

            loop_results[-1]["status"] = "success"

            # 每次成功执行后检查是否已达到目标
            if i == iterations - 1:
                continue

        return loop_results

    def summary(self) -> str:
        return (
            f"LoopEngine({self.name}): "
            f"{self.execution_count} executions, "
            f"{len(self._history)} in history"
        )
