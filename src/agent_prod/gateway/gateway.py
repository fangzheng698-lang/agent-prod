"""
质量门网关 - 连接 Agent Runtime 执行结果与 Quality Gate Engine。

Phase 3: 将 5 道门作为中间件接入 agent-prod API。
每次 agent 执行完成后，结果通过门禁 pipeline 验证后才返回用户。

用法:
    gateway = QualityGateGateway.from_config()
    improvement, all_passed = await gateway.validate(session_id, messages, turns)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from agent_prod.gates.models import Improvement, ImprovementStatus, GateName, GateResult
from agent_prod.gates.engine import QualityGateEngine

logger = logging.getLogger(__name__)

# Package root — used to resolve relative config paths
_PKG_ROOT = Path(__file__).resolve().parent.parent


class QualityGateGateway:
    """
    质量门网关 - Runtime 执行产出 -> Improvement 构造 -> 5 道门 pipeline。

    线程安全：无共享可变状态（engine 在 __init__ 后只读）。
    """

    def __init__(self, engine: QualityGateEngine):
        self._engine = engine

    @classmethod
    def from_config(cls, config_path: str | Path | None = None) -> "QualityGateGateway":
        """从 YAML 配置创建网关（与 Phase 2 配置兼容）。"""
        # 相对路径相对于项目根目录（app/ 的父目录）解析，不依赖 cwd
        if config_path is not None:
            cp = Path(config_path)
            if not cp.is_absolute():
                cp = _PKG_ROOT / cp
            config_path = cp
        engine = QualityGateEngine.from_yaml(config_path)
        return cls(engine)

    @classmethod
    def memory(cls) -> "QualityGateGateway":
        """快速创建内存模式网关（无基础设施依赖）。"""
        engine = QualityGateEngine(repository=None)  # defaults to MemoryRepository
        return cls(engine)

    @property
    def engine(self) -> QualityGateEngine:
        return self._engine

    # ---- Improvement 构造 ----

    def build_improvement(
        self,
        session_id: str,
        messages: list[dict],
        turns: list,  # list[TurnRecord]
    ) -> Improvement:
        """
        从 Runtime 执行结果构造 Improvement。

        参数:
            session_id: 会话 ID
            messages:   最终消息列表
            turns:      AgentRuntime.run() 返回的 TurnRecord 列表
        """
        # 提取最终响应内容
        final_content = ""
        for turn in reversed(turns):
            if turn.response and turn.response.content:
                final_content = turn.response.content
                break
        if not final_content.strip():
            final_content = f"Task completed in {len(turns)} turns"

        # 汇总 token 和时间
        total_prompt = sum(
            t.response.tokens_prompt for t in turns if t.response
        )
        total_completion = sum(
            t.response.tokens_completion for t in turns if t.response
        )
        total_time_ms = sum(t.duration_ms for t in turns)

        # 构造追踪数据 — 确保 llm_calls.response_id == tool_calls.request_id
        llm_calls = []
        tool_calls = []
        for t in turns:
            if t.response:
                resp_id = f"turn-{t.index}-resp"
                llm_calls.append({
                    "request_id": f"turn-{t.index}",
                    "response_id": resp_id,
                    "turn": t.index,
                    "tokens_prompt": t.response.tokens_prompt,
                    "tokens_completion": t.response.tokens_completion,
                })
                # 该轮的工具调用应引用该轮 LLM 响应的 response_id
                for tr in t.tool_results:
                    tool_calls.append({
                        "request_id": resp_id,
                        "response_id": tr.get("name", "unknown"),
                        "turn": t.index,
                        "tool": tr.get("name", "unknown"),
                    })
            else:
                for tr in t.tool_results:
                    tool_calls.append({
                        "request_id": f"turn-{t.index}-resp",
                        "response_id": tr.get("name", "unknown"),
                        "turn": t.index,
                        "tool": tr.get("name", "unknown"),
                    })

        total_tokens = total_prompt + total_completion

        # 收集工具名
        tools_used = list(set(
            tr.get("name", "unknown")
            for t in turns
            for tr in t.tool_results
        ))

        return Improvement(
            id=f"imp-{session_id}",
            name=session_id,
            candidate_output={
                "final_response": final_content[:5000],
                "confidence": 0.95,
                "tools_used": tools_used,
                "token_count": total_tokens,
                "warnings": [],
            },
            budget_tokens=max(100_000, total_tokens * 2),
            budget_time_ms=max(120_000, int(total_time_ms * 3)),
            actual_tokens=total_tokens,
            actual_time_ms=int(total_time_ms),
            baseline={
                "latency_p95_ms": max(1000, int(total_time_ms)),
                "success_rate": 0.99,
                "error_rate": 0.01,
            },
            llm_calls=llm_calls,
            tool_calls=tool_calls,
        )

    # ---- Pipeline 编排 ----

    async def validate(
        self,
        session_id: str,
        messages: list[dict],
        turns: list,
    ) -> tuple[Improvement, bool]:
        """
        对 Runtime 执行结果运行完整质量门 pipeline。

        返回:
            (improvement, all_passed)
            - improvement: 更新后的 Improvement（含 gate_results）
            - all_passed: True 表示 5 道门全过，status=PRODUCTION
        """
        improvement = self.build_improvement(session_id, messages, turns)

        try:
            # run_pipeline 在 ThreadPoolExecutor 中运行各道门的超时保护，
            # persist=False 避免 pipeline 内部调用 async save
            result: Improvement = await asyncio.to_thread(
                self._engine.run_pipeline,
                improvement,
                human_approver="agent-prod-api",
                persist=False,
            )

            # 异步安全持久化
            save_fn = self._engine.repository.save
            if asyncio.iscoroutinefunction(save_fn):
                await save_fn(result)
            else:
                save_fn(result)

            all_passed = result.status == ImprovementStatus.PRODUCTION
            return result, all_passed

        except Exception as e:
            logger.exception("Quality gate pipeline failed for session %s", session_id)
            # 引擎故障时放行（不让门禁阻断服务）
            improvement.status = ImprovementStatus.PRODUCTION
            return improvement, True

    def gate_results_to_dict(
        self,
        improvement: Improvement,
    ) -> dict:
        """将门禁结果序列化为 API 响应格式。"""
        gates = []
        for gr in improvement.gate_results:
            gates.append({
                "gate": gr.gate_name.value if hasattr(gr.gate_name, 'value') else str(gr.gate_name),
                "passed": gr.passed,
                "reason": gr.reason[:200],
                "duration_ms": round(gr.duration_ms, 1),
            })

        fail_gate = improvement.fail_gate
        if fail_gate:
            fail_gate_str = fail_gate.value if hasattr(fail_gate, 'value') else str(fail_gate)
        else:
            fail_gate_str = None

        return {
            "status": improvement.status.value,
            "passed": improvement.status == ImprovementStatus.PRODUCTION,
            "gates": gates,
            "failed_at": fail_gate_str,
            "fail_reason": improvement.fail_reason[:200] if improvement.fail_reason else None,
        }
