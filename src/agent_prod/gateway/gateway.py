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
import inspect
import logging
from pathlib import Path

from agent_prod.gates.engine import QualityGateEngine
from agent_prod.gates.models import Improvement, ImprovementStatus

logger = logging.getLogger(__name__)

# Package root — used to resolve relative config paths
_PKG_ROOT = Path(__file__).resolve().parent.parent


def _serialize_decisions(decisions: list) -> list[dict]:
    """将 AgentTrace Decision 列表序列化为 dict，供 Gate0 权限检查使用。


    gate0_permission 需要: decisions[i]["tool_calls"][j]["tool_name"]
    """
    result = []
    for d in decisions:
        # d can be a Decision dataclass or a dict already
        if isinstance(d, dict):
            decision_id = d.get("decision_id", "")
            tool_calls = d.get("tool_calls", [])
        else:
            decision_id = getattr(d, "decision_id", "")
            tool_calls = getattr(d, "tool_calls", [])

        tc_dicts = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tc_dicts.append(tc)
            else:
                tc_dicts.append({
                    "tool_id": getattr(tc, "tool_id", ""),
                    "tool_name": getattr(tc, "tool_name", ""),
                    "success": getattr(tc, "success", True),
                    "arguments": getattr(tc, "arguments", {}),
                })
        result.append({
            "decision_id": decision_id,
            "tool_calls": tc_dicts,
        })
    return result


class QualityGateGateway:
    """
    质量门网关 - Runtime 执行产出 -> Improvement 构造 -> 5 道门 pipeline。

    线程安全：无共享可变状态（engine 在 __init__ 后只读）。
    """

    def __init__(self, engine: QualityGateEngine):
        self._engine = engine

    @classmethod
    def from_config(cls, config_path: str | Path | None = None) -> QualityGateGateway:
        """从 YAML 配置创建网关（与 Phase 2 配置兼容）。"""
        # 相对路径先尝试 cwd，再尝试包根目录
        if config_path is not None:
            cp = Path(config_path)
            if not cp.is_absolute():
                if not cp.exists():
                    cp = _PKG_ROOT / cp
            config_path = cp
        engine = QualityGateEngine.from_yaml(config_path)
        return cls(engine)

    @classmethod
    def memory(cls) -> QualityGateGateway:
        """快速创建内存模式网关。

        Note: "memory" 指零外部基础设施依赖，但评估结果仍会持久化到
        data/improvements.json（FileRepository）。重启不丢数据。
        """
        from pathlib import Path
        from agent_prod.gates.engine import load_config
        from agent_prod.gates.repository import FileRepository
        config = load_config()  # loads default config.yaml with per-agent thresholds
        # 默认持久化路径：项目 data 目录下的 improvements.json
        repo_path = Path("data") / "improvements.json"
        repo = FileRepository(str(repo_path))
        engine = QualityGateEngine(config=config, repository=repo)
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
            # persist=False 避免 pipeline 内部调用 async save。
            # 外层 asyncio.wait_for 防止 _pool_executor worker 卡死，把整个
            # event-loop 调用方也卡住（engine 内 pipeline_timeout=180s 仅
            # 唤醒自身 future，event-loop 的 to_thread 协程仍在等）。
            engine = self._engine
            pipeline_timeout = float(engine.config.get("pipeline_timeout_seconds", 180.0))
            # 外层略宽松，给 engine 内部的 pipeline_timeout 先触发留余量
            outer_timeout = pipeline_timeout + 5.0
            result: Improvement = await asyncio.wait_for(
                asyncio.to_thread(
                    engine.run_pipeline,
                    improvement,
                    human_approver="agent-prod-api",
                    persist=False,
                ),
                timeout=outer_timeout,
            )

            # 异步安全持久化
            save_fn = self._engine.repository.save
            if inspect.iscoroutinefunction(save_fn):
                await save_fn(result)
            else:
                save_fn(result)

            all_passed = result.status == ImprovementStatus.PRODUCTION
            return result, all_passed

        except (asyncio.TimeoutError, OSError) as e:
            # Transient errors — don't block traffic, but don't promote either
            logger.warning("Gate pipeline transient error for session %s: %s", session_id, e)
            improvement.status = ImprovementStatus.REJECTED
            improvement.fail_gate = "pipeline"
            improvement.fail_reason = f"Gate pipeline transient error: {e}"
            return improvement, False
        except Exception:
            logger.exception("Quality gate pipeline failed for session %s", session_id)
            # Check fail_open config — default False (safe)
            fail_open = self._engine.config.get("gateway", {}).get("fail_open_on_error", False)
            if fail_open:
                improvement.status = ImprovementStatus.PRODUCTION
                return improvement, True
            improvement.status = ImprovementStatus.REJECTED
            improvement.fail_gate = "pipeline"
            improvement.fail_reason = "Internal error during gate evaluation — REJECTED"
            return improvement, False

    def gate_results_to_dict(
        self,
        improvement: Improvement,
    ) -> dict:
        """将门禁结果序列化为 API 响应格式。"""
        gates = []
        for gr in improvement.gate_results:
            gate_entry = {
                "gate": gr.gate_name.value if hasattr(gr.gate_name, 'value') else str(gr.gate_name),
                "passed": gr.passed,
                "reason": gr.reason[:200],
                "duration_ms": round(gr.duration_ms, 1),
            }
            # 透传门禁详细数据（跳过内部 key）
            if gr.details:
                skip_keys = {"_internal"}
                filtered = {k: v for k, v in gr.details.items()
                           if not k.startswith("_") and k not in skip_keys}
                if filtered:
                    gate_entry["details"] = filtered
            gates.append(gate_entry)

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

    # ── AgentTrace 接入 ──────────────────────────────────────

    async def evaluate_agent_trace(
        self,
        trace,  # AgentTrace
    ) -> tuple[Improvement, bool, float]:
        """
        对外部 agent trace 执行 5 道门验证。

        流程：
          1. 根据 trace.agent 查找 adapter
          2. adapter 将 AgentTrace → Improvement
          3. 投入门禁 pipeline
          4. 返回 (improvement, all_passed, duration_ms)

        参数:
            trace: AgentTrace 实例
        返回:
            (improvement, all_passed, duration_ms)
        """
        import time as _time
        _start = _time.monotonic()

        from agent_prod.trace.adapters import ADAPTER_REGISTRY

        # 查找 adapter
        adapter = ADAPTER_REGISTRY.get(trace.agent)

        # 转换
        improvement = adapter.to_improvement(trace)
        if improvement is None:
            # 无法评估 — 返回一个标记为 rejected 的 Improvement
            from agent_prod.gates.models import GateName
            improvement = Improvement(
                id=f"imp-{trace.session_id}",
                name=trace.session_id,
                status=ImprovementStatus.REJECTED,
                fail_gate=GateName.GATE1.value,
                fail_reason=f"Cannot evaluate: adapter for '{trace.agent}' returned None",
                metadata={"agent": trace.agent, "error": "incomplete_trace"},
            )
            duration_ms = (_time.monotonic() - _start) * 1000
            return improvement, False, duration_ms

        # ── 注入 agent 类型和 decisions 到 metadata 供 Gate0 使用 ──
        improvement.metadata["agent"] = trace.agent
        improvement.metadata["decisions"] = _serialize_decisions(trace.decisions)
        improvement.metadata["declared_tools"] = getattr(trace, "declared_tools", [])
        improvement.metadata["auth_grant_id"] = getattr(trace, "auth_grant_id", "")

        try:
            # 外层 asyncio.wait_for 守护 engine 内 ThreadPoolExecutor：若 worker
            # 长期不返回（engine 内 future 断但实际线程卡住），外层依然能在
            # outer_timeout 后取消 to_thread 协程，让 event-loop 调用方及时返回。
            engine = self._engine
            pipeline_timeout = float(engine.config.get("pipeline_timeout_seconds", 180.0))
            outer_timeout = pipeline_timeout + 5.0
            result: Improvement = await asyncio.wait_for(
                asyncio.to_thread(
                    engine.run_pipeline,
                    improvement,
                    human_approver=trace.human_approver or "external-agent",
                    persist=False,
                ),
                timeout=outer_timeout,
            )

            save_fn = self._engine.repository.save
            if inspect.iscoroutinefunction(save_fn):
                await save_fn(result)
            else:
                save_fn(result)

            all_passed = result.status == ImprovementStatus.PRODUCTION
            duration_ms = (_time.monotonic() - _start) * 1000
            return result, all_passed, duration_ms

        except asyncio.CancelledError:
            raise
        except OSError as e:
            # 磁盘/网络/文件系统错误 — 可能 transient，记录并拒绝本次评估
            logger.warning("Pipeline I/O error for %s/%s: %s",
                           trace.agent, trace.session_id, e)
            improvement.status = ImprovementStatus.REJECTED
            improvement.fail_gate = "pipeline"
            improvement.fail_reason = f"I/O error during evaluation: {e}"
            duration_ms = (_time.monotonic() - _start) * 1000
            return improvement, False, duration_ms
        except Exception:
            # 未知内部错误 — 安全起见于 REJECT，不强制 PRODUCTION
            logger.exception("Agent trace evaluation failed for %s/%s",
                             trace.agent, trace.session_id)
            improvement.status = ImprovementStatus.REJECTED
            improvement.fail_gate = "pipeline"
            improvement.fail_reason = "Internal error during gate evaluation — REJECTED for safety"
            duration_ms = (_time.monotonic() - _start) * 1000
            return improvement, False, duration_ms
