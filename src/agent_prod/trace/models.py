"""
AgentTrace — 统一 agent trace 格式。

所有 agent（Hermes / Claude Code / Codex / OpenCode 等）执行完毕后，
将执行数据打包成 AgentTrace，POST 到 /v1/agent/evaluate，
agent-prod 过 5 道质量门后返回 pass/gray/reject 决策。

设计原则：
  只包含门禁需要的最小信息集。不是 agent 原始 trace 的全量镜像，
  而是门禁中间表示 —— 每个字段都对应一道门的验证需求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── 枚举 ──────────────────────────────────────────────────────────

class AgentType(str, Enum):
    """agent-prod 已知的 agent 类型。adapter 注册表键。"""
    HERMES = "hermes"
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    OPENCODE = "opencode"
    GENERIC = "generic"       # fallback，按通用逻辑映射


class TrafficStage(str, Enum):
    """灰度流量阶梯。"""
    NONE = "none"
    P1 = "1%"
    P10 = "10%"
    P50 = "50%"
    P100 = "100%"


class PolicyTag(str, Enum):
    """策略标签 — Gate5 审计分类。"""
    PRODUCTION = "production"
    STAGING = "staging"
    EXPERIMENTAL = "experimental"
    INTERNAL = "internal"
    SANDBOXED = "sandboxed"


# ── 子结构 ──────────────────────────────────────────────────────

@dataclass
class ToolInvocation:
    """单次工具调用记录。"""
    tool_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    success: bool = True
    duration_ms: float = 0.0


@dataclass
class Decision:
    """单次 LLM 决策（turn）。"""
    decision_id: str
    model: str                          # "gpt-4", "claude-3.5-sonnet"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    reasoning: str = ""                 # CoT / thinking


@dataclass
class MetricsSnapshot:
    """性能指标快照 — Gate3 回归对比 + 动态基线。"""
    latency_p95_ms: float = 0.0
    success_rate: float = 1.0
    error_rate: float = 0.0
    token_efficiency: float = 1.0         # 相对预算的 token 使用效率
    custom: dict[str, Any] = field(default_factory=dict)  # agent 自定义指标


@dataclass
class TrafficMetrics:
    """
    灰度发布流量指标。

    Gate4 (gray release) 需要：
      - 当前流量阶梯
      - 该阶梯下的错误率/延迟/资源消耗
      - 与基线的差异
    """
    stage: TrafficStage = TrafficStage.NONE
    request_count: int = 0
    error_rate: float = 0.0
    latency_p95_ms: float = 0.0
    resource_usage_pct: float = 0.0       # CPU/内存使用百分比


# ── 主结构 ──────────────────────────────────────────────────────

@dataclass
class AgentTrace:
    """
    统一 agent trace —— 任何 agent 都可以产出此结构。

    用 AgentTraceAdapter 将其转换为 Improvement 后投入门禁管线。

    示例:
        AgentTrace(
            agent="my-agent",
            version="v0.2.0",
            session_id="ses_abc123",
            output={"final_response": "...", "tools_used": ["search"]},
            current_metrics=MetricsSnapshot(latency_p95_ms=1200, success_rate=0.98),
            decisions=[Decision(decision_id="turn-1", tool_calls=[...])],
        )
    """
    # ── 身份 ──
    agent: str                            # "hermes" | "claude-code" | ...
    version: str                          # agent 版本号
    session_id: str                       # 会话 ID

    # ── Gate1 执行合约 ──
    output: dict[str, Any]                # 最终输出（结构待 gate1 验证）
    output_schema: str | None = None      # 期望的输出结构名（optional）

    # ── Gate2 轨迹完整性 ──
    decisions: list[Decision] = field(default_factory=list)

    # ── Gate3 回归 ──
    baseline_metrics: MetricsSnapshot | None = None
    current_metrics: MetricsSnapshot = field(default_factory=MetricsSnapshot)

    # ── Gate4 灰度 ──
    traffic: TrafficMetrics | None = None
    traffic_percentage: int = 0          # 直接指定流量百分比（用于状态机）

    # ── Gate5 审计 ──
    human_approver: str = ""              # 审批人标识
    policy_tags: list[str] = field(default_factory=list)  # 策略标签

    # ── 预算 ──
    budget_tokens: int = 100_000
    budget_time_ms: int = 120_000

    # ── 元数据 ──
    trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Gate0 权限 ──
    declared_tools: list[str] = field(default_factory=list)
    auth_grant_id: str = ""   # 用户授权 ID（针对 dangerous 操作的授权凭证）

    def total_tokens(self) -> int:
        """汇总所有决策的 token 消耗"""
        return sum(
            d.prompt_tokens + d.completion_tokens
            for d in self.decisions
        )

    def total_time_ms(self) -> float:
        """汇总所有工具调用的耗时"""
        return sum(
            tc.duration_ms
            for d in self.decisions
            for tc in d.tool_calls
        )

    def all_tool_names(self) -> list[str]:
        """收集所有使用过的工具名（去重）"""
        seen = set()
        result = []
        for d in self.decisions:
            for tc in d.tool_calls:
                if tc.tool_name not in seen:
                    seen.add(tc.tool_name)
                    result.append(tc.tool_name)
        return result


# ── API 响应 ────────────────────────────────────────────────────

@dataclass
class EvaluateResult:
    """
    /v1/agent/evaluate 的响应。

    agent 拿到这个结果后：
      - status == "production" → 放行
      - status == "gray"       → 进入灰度阶梯
      - status == "rejected"   → 拦截，看 fail_reason
    """
    agent: str
    session_id: str
    status: str                         # "production" | "rejected" | "gray" | ...
    passed: bool
    gates: list[dict[str, Any]]          # 各道门的详细结果
    failed_at: str | None = None
    fail_reason: str | None = None
    total_duration_ms: float = 0.0
