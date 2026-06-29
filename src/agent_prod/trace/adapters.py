"""
AgentTraceAdapter — 将 AgentTrace 转换为 Improvement 的接口。

gateway 调用 from_agent_trace(trace) → adapter 返回 Improvement。
每种 agent 注册一个 adapter，键为 agent 类型字符串。

要接入新 agent：写一个 Adapter 子类，实现 to_improvement()，注册即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from agent_prod.gates.models import Improvement, ImprovementStatus

from .models import (
    AgentTrace,
    AgentType,
    Decision,
    MetricsSnapshot,
)


class AgentTraceAdapter(ABC):
    """
    将 AgentTrace 映射为门禁引擎所需的 Improvement。

    子类实现 to_improvement()，处理 agent 特有的字段映射逻辑。
    如果 to_improvement() 返回 None，表示 trace 数据不足以评估。
    """

    # 子类声明支持的 agent 类型
    supports: ClassVar[list[str]] = []

    @abstractmethod
    def to_improvement(self, trace: AgentTrace) -> Improvement | None:
        """
        将 AgentTrace 转换为 Improvement，投入门禁管线。

        返回 None 表示数据不完整、无法评估。
        """
        ...

    def map_decisions_to_trace_pairs(
        self, decisions: list[Decision],
    ) -> tuple[list[dict], list[dict]]:
        """
        通用映射：Decision 列表 → (llm_calls, tool_calls)。

        llm_calls 的 response_id == tool_calls 的 request_id，
        满足 gate2 的 DAG 验证要求。
        """
        llm_calls = []
        tool_calls = []

        for d in decisions:
            llm_calls.append({
                "request_id": d.decision_id,
                "response_id": d.decision_id,
                "model": d.model,
                "tokens_prompt": d.prompt_tokens,
                "tokens_completion": d.completion_tokens,
                "duration_ms": 0.0,  # will be refined if available
            })
            for tc in d.tool_calls:
                tool_calls.append({
                    "request_id": d.decision_id,
                    "response_id": tc.tool_id,
                    "tool": tc.tool_name,
                    "success": tc.success,
                    "duration_ms": tc.duration_ms,
                })

        return llm_calls, tool_calls

    def map_metrics_to_baseline_candidate(
        self,
        baseline: MetricsSnapshot | None,
        current: MetricsSnapshot,
        raw_output: dict | str | None = None,
    ) -> tuple[dict, dict]:
        """将 MetricsSnapshot 转为 baseline_output / candidate_output 字典"""
        b = {
            "latency_p95_ms": baseline.latency_p95_ms,
            "success_rate": baseline.success_rate,
            "error_rate": baseline.error_rate,
        } if baseline else {}

        # ── final_response: 从 raw_output 获取真实回复 ──
        final_resp = "trace-evaluation"  # fallback
        if isinstance(raw_output, str) and raw_output.strip():
            final_resp = raw_output[:5000]
        elif isinstance(raw_output, dict):
            fr = raw_output.get("final_response", "") or raw_output.get("output", "")
            if fr:
                final_resp = str(fr)[:5000]

        c = {
            "final_response": final_resp,
            "confidence": 0.95,
            "tools_used": [],
            "token_count": 0,
            "warnings": [],
            "latency_p95_ms": current.latency_p95_ms,
            "success_rate": current.success_rate,
            "error_rate": current.error_rate,
            "f1_score": current.success_rate,       # gate3 需要 f1_score 字段
        }
        # 合并自定义指标
        if baseline:
            for k, v in baseline.custom.items():
                b[k] = v
        for k, v in current.custom.items():
            c[k] = v

        return b, c


# ═══════════════════════════════════════════════════════════════
# 内置 adapters
# ═══════════════════════════════════════════════════════════════


def _decisions_for_gate0(decisions: list) -> list[dict]:
    """将 AgentTrace.Decision 列表转为 gate0 可读的 decisions dict.

    递归转换 tool_calls 中的 ToolInvocation dataclass 为 dict。
    """
    def _to_dict(obj):
        if hasattr(obj, '__dataclass_fields__'):
            return {k: _to_dict(v) for k, v in obj.__dict__.items() if not k.startswith('_')}
        if isinstance(obj, dict):
            return {k: _to_dict(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_to_dict(v) for v in obj]
        return obj

    result = []
    for d in decisions:
        if hasattr(d, '__dataclass_fields__') or hasattr(d, '__dict__'):
            d_dict = _to_dict(d)
        elif isinstance(d, dict):
            d_dict = d
        else:
            continue
        result.append(d_dict)
    return result


class GenericAdapter(AgentTraceAdapter):
    """通用 adapter — fallback，对所有 agent 类型都适用。

    不需要声明 supports。当 AdapterRegistry.get() 找不到专用 adapter 时，
    自动 fallback 到 GenericAdapter。
    """

    supports: ClassVar[list[str]] = []  # 不通过 supports 注册；由 registry 自动 fallback

    def to_improvement(self, trace: AgentTrace) -> Improvement | None:
        llm_calls, tool_calls = self.map_decisions_to_trace_pairs(trace.decisions)
        baseline, candidate = self.map_metrics_to_baseline_candidate(
            trace.baseline_metrics, trace.current_metrics,
            raw_output=trace.output,
        )

        # 填充工具列表
        candidate["tools_used"] = trace.all_tool_names()
        candidate["token_count"] = trace.total_tokens()

        return Improvement(
            id=f"imp-{trace.session_id}",
            name=trace.session_id,
            status=ImprovementStatus.CANDIDATE,
            baseline_output=baseline,
            candidate_output=candidate,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            actual_tokens=trace.total_tokens(),
            actual_time_ms=int(trace.total_time_ms()),
            budget_tokens=trace.budget_tokens,
            budget_time_ms=trace.budget_time_ms,
            trace_id=trace.trace_id,
            human_approver=trace.human_approver,
            traffic_percentage=(
                trace.traffic_percentage if trace.traffic_percentage > 0
                else 1 if trace.traffic and trace.traffic.stage.value == "1%"
                else 10 if trace.traffic and trace.traffic.stage.value == "10%"
                else 50 if trace.traffic and trace.traffic.stage.value == "50%"
                else 100 if trace.traffic and trace.traffic.stage.value == "100%"
                else 0
            ),
            metadata={
                "agent": trace.agent,
                "agent_version": trace.version,
                "policy_tags": trace.policy_tags,
                "gray_release_active": trace.traffic is not None,   # ← gate4 信号
                "declared_tools": trace.declared_tools,             # ← gate0 信号
                "auth_grant_id": trace.auth_grant_id,               # ← gate0 信号
                "decisions": _decisions_for_gate0(trace.decisions), # ← gate0 信号
                **trace.metadata,
            },
        )


class HermesAdapter(AgentTraceAdapter):
    """
    Hermes Agent 专用 adapter。

    从 Hermes 的 TurnRecord 格式直接映射。
    Hermes trace 的特征：decisions 按 turn 编号，包含 system/user/assistant 完整对话。
    """

    supports: ClassVar[list[str]] = ["hermes"]

    def to_improvement(self, trace: AgentTrace) -> Improvement | None:
        # 复用通用映射逻辑，加 Hermes 特有的元数据
        imp = GenericAdapter().to_improvement(trace)
        if imp is None:
            return None

        # Hermes 特有：标记 skill 使用
        skills_used = []
        for d in trace.decisions:
            for tc in d.tool_calls:
                if tc.tool_name in ("skill_view", "skill_manage", "skills_list"):
                    skills_used.append(tc.arguments.get("name", "unknown"))
        if skills_used:
            imp.metadata["skills_used"] = skills_used

        # Hermes 特有：记忆操作计数
        memory_ops = sum(
            1 for d in trace.decisions
            for tc in d.tool_calls
            if tc.tool_name == "memory"
        )
        imp.metadata["memory_operations"] = memory_ops

        return imp


class ClaudeCodeAdapter(AgentTraceAdapter):
    """
    Claude Code CLI 专用 adapter。

    Claude Code trace 特征：decisions 对应 claude_sdk 的 messages，
    tool_calls 包含 tool_use 块。
    """

    supports: ClassVar[list[str]] = ["claude-code"]

    def to_improvement(self, trace: AgentTrace) -> Improvement | None:
        return GenericAdapter().to_improvement(trace)


class CodexAdapter(AgentTraceAdapter):
    """OpenAI Codex CLI 专用 adapter。"""

    supports: ClassVar[list[str]] = ["codex"]

    def to_improvement(self, trace: AgentTrace) -> Improvement | None:
        return GenericAdapter().to_improvement(trace)


# ═══════════════════════════════════════════════════════════════
# Adapter 注册表
# ═══════════════════════════════════════════════════════════════


class AdapterRegistry:
    """
    全局 adapter 注册表。

    用法:
        registry = AdapterRegistry()
        registry.register(MyAdapter())

        adapter = registry.get("my-agent")
        improvement = adapter.to_improvement(trace)
    """

    def __init__(self):
        self._adapters: dict[str, AgentTraceAdapter] = {}

    def register(self, adapter: AgentTraceAdapter) -> None:
        for agent_type in adapter.supports:
            key = agent_type.value if hasattr(agent_type, 'value') else agent_type
            self._adapters[key] = adapter

    def get(self, agent_type: str) -> AgentTraceAdapter:
        """获取 adapter；未注册的返回 GenericAdapter。"""
        return self._adapters.get(agent_type, GenericAdapter())

    def list_agents(self) -> list[str]:
        return sorted(self._adapters.keys())


# 全局单例 — 专用 adapter 注册。GenericAdapter 作为 get() 的 fallback
ADAPTER_REGISTRY = AdapterRegistry()
ADAPTER_REGISTRY.register(HermesAdapter())
ADAPTER_REGISTRY.register(ClaudeCodeAdapter())
ADAPTER_REGISTRY.register(CodexAdapter())
