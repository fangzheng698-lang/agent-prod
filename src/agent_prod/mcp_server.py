"""
agent-prod MCP Server — 将 agent-prod 质量门 (Gate0-Gate7) 暴露为 MCP 工具。

任何 MCP 兼容的 agent (Claude Desktop, Cursor, Hermes 等) 可以调用这些工具
对 agent trace 进行完整的质量门评估。

工具:
  - evaluate_trace     完整的 Gate0-Gate7 质量门评估
  - check_tool_safety  单次工具调用的 Gate0 前置安全检查
  - get_gate_stats     查询历史评估统计
  - health_check       引擎和仓库健康检查

用法:
  agent-prod-mcp
  python -m agent_prod.mcp_server
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("agent-prod-mcp")

mcp = FastMCP(
    "agent-prod",
    instructions="agent-prod Quality Gates — Gate0-Gate7 evaluation for AI agent traces",
)

# ── Lazy engine ────────────────────────────────────────────────

_engine = None
_engine_lock = None


def _get_engine():
    global _engine, _engine_lock
    if _engine is not None:
        return _engine

    import threading
    if _engine_lock is None:
        _engine_lock = threading.Lock()

    with _engine_lock:
        if _engine is not None:
            return _engine

        from agent_prod.gates.engine import QualityGateEngine
        from agent_prod.gates.repository import FileRepository

        file_path = os.environ.get(
            "AGENT_PROD_REPO",
            "/var/lib/quality_gates/improvements.json",
        )
        repo = FileRepository(file_path)
        _engine = QualityGateEngine(repository=repo)
        logger.info("QualityGateEngine ready (repo=%s, %d records)",
                     file_path, repo.count())
        return _engine


# ═══════════════════════════════════════════════════════════════
#  evaluate_trace
# ═══════════════════════════════════════════════════════════════


@mcp.tool()
def evaluate_trace(
    agent: str = "generic",
    session_id: str = "",
    decisions: list[dict] | None = None,
    current_metrics: dict | None = None,
    baseline_metrics: dict | None = None,
    traffic_percentage: int = 1,
    human_approver: str = "mcp",
    declared_tools: list[str] | None = None,
    budget_tokens: int = 100_000,
    budget_time_ms: int = 120_000,
    gate7_mode: str = "observe",
) -> dict[str, Any]:
    """完整的 Gate0-Gate7 质量门评估。

    传入 agent trace（工具调用、LLM 决策、性能指标），返回各道门的
    通过/失败结果。用于在发布前判断一次 agent 运行是否可以进入生产。

    Args:
        agent: Agent 类型 (如 'hermes', 'claude-code')，影响 per-agent 阈值。
        session_id: 唯一会话标识（空则自动生成）。
        decisions: LLM 决策列表，每项含 model, tokens, tool_calls。
        current_metrics: 含 latency_p95_ms, success_rate, final_response, expected_answer 等。
        baseline_metrics: 可选回归对比基准。
        traffic_percentage: 灰度流量百分比 (1-100)。
        human_approver: 审批人标识。
        declared_tools: agent 声明使用的工具 (如 ['read_file', 'web_search'])。
        budget_tokens: Token 预算上限。
        budget_time_ms: 时间预算上限 (毫秒)。
        gate7_mode: 'observe' 或 'enforce'。
    """
    engine = _get_engine()
    decisions = decisions or []
    current_metrics = current_metrics or {}
    declared_tools = declared_tools or []

    sid = session_id or f"ses_{uuid.uuid4().hex[:12]}"

    # 组装 candidate_output
    candidate = dict(current_metrics.get("custom", {}))
    for key in (
        "latency_p95_ms", "success_rate", "f1_score", "accuracy",
        "bleu", "rouge_l", "final_response", "expected_answer",
        "user_question",
    ):
        if key in current_metrics and key not in candidate:
            candidate[key] = current_metrics[key]

    # 组装 tool_calls & llm_calls（Gate2 轨迹完整性需要两者配对）
    tool_calls: list[dict] = []
    llm_calls: list[dict] = []
    for d in decisions:
        did = d.get("decision_id", str(uuid.uuid4().hex[:8]))
        llm_calls.append({
            "request_id": did,
            "response_id": did,
            "model": d.get("model", "unknown"),
            "prompt_tokens": d.get("prompt_tokens", 0),
            "completion_tokens": d.get("completion_tokens", 0),
            "duration_ms": d.get("duration_ms", 1000),
            "finish_reason": d.get("finish_reason", "stop"),
        })
        for tc in d.get("tool_calls", []):
            tool_calls.append({
                "tool_id": tc.get("tool_id", ""),
                "tool_name": tc.get("tool_name", ""),
                "arguments": tc.get("arguments", {}),
                "success": tc.get("success", True),
                "duration_ms": tc.get("duration_ms", 0),
                "request_id": did,
            })

    metadata = {
        "agent": agent,
        "declared_tools": declared_tools,
        "decisions": decisions,
        "source": "mcp_server",
        "gate7_mode": gate7_mode,
    }

    from agent_prod.gates.models import Improvement

    improvement = Improvement(
        name=f"mcp-{sid}",
        id=f"imp-{sid}",
        candidate_output=candidate,
        baseline_output=baseline_metrics or {},
        metadata=metadata,
        budget_tokens=budget_tokens,
        budget_time_ms=budget_time_ms,
        traffic_percentage=traffic_percentage,
        human_approver=human_approver,
        tool_calls=tool_calls,
        llm_calls=llm_calls,
    )

    engine.run_pipeline(improvement)

    gates_summary = []
    for gr in improvement.gate_results:
        gates_summary.append({
            "gate": gr.gate_name.value,
            "passed": gr.passed,
            "reason": gr.reason,
            "duration_ms": round(gr.duration_ms, 1),
        })

    return {
        "agent": agent,
        "session_id": sid,
        "passed": all(g["passed"] for g in gates_summary),
        "status": improvement.status.value,
        "failed_at": improvement.fail_gate,
        "fail_reason": improvement.fail_reason,
        "gates_passed": sum(1 for g in gates_summary if g["passed"]),
        "total_gates": len(gates_summary),
        "gates": gates_summary,
    }


# ═══════════════════════════════════════════════════════════════
#  check_tool_safety
# ═══════════════════════════════════════════════════════════════


@mcp.tool()
def check_tool_safety(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    agent: str = "generic",
    declared_tools: list[str] | None = None,
) -> dict[str, Any]:
    """单次工具调用的 Gate0 前置安全检查。

    在实际调用工具前检查：工具是否已声明、风险等级、参数是否存在威胁。
    MCP agent 可以用此工具在每次 tool call 前做预检。

    Args:
        tool_name: 工具名 (如 'read_file', 'terminal')。
        arguments: 工具参数 (如 {'path': '/etc/passwd'})。
        agent: Agent 类型，影响 trusted-agent 豁免和 per-agent 配置。
        declared_tools: agent 事先声明的工具列表。
    """
    engine = _get_engine()
    arguments = arguments or {}
    declared_tools = declared_tools or [tool_name]

    metadata = {
        "agent": agent,
        "declared_tools": declared_tools,
        "decisions": [{
            "decision_id": "preflight",
            "tool_calls": [{"tool_name": tool_name, "arguments": arguments}],
        }],
        "source": "mcp_preflight",
    }

    from agent_prod.gates.models import Improvement

    improvement = Improvement(
        name=f"preflight-{tool_name}",
        id=f"imp-pf-{uuid.uuid4().hex[:8]}",
        candidate_output={"tools_used": [tool_name]},
        metadata=metadata,
        tool_calls=[{"tool_name": tool_name, "arguments": arguments}],
    )

    g0_result = engine.run_gate(improvement, "gate0_permission")
    details = g0_result.details

    return {
        "tool_name": tool_name,
        "agent": agent,
        "passed": g0_result.passed,
        "reason": g0_result.reason,
        "risk_level": details.get("risk_level", "unknown") if isinstance(details, dict) else "unknown",
        "blocked": details.get("blocked", 0) if isinstance(details, dict) else 0,
        "violations": details.get("violations", []) if isinstance(details, dict) else [],
    }


# ═══════════════════════════════════════════════════════════════
#  get_gate_stats
# ═══════════════════════════════════════════════════════════════


@mcp.tool()
def get_gate_stats(
    agent_filter: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """查询质量门仓库的历史评估统计。

    Args:
        agent_filter: 按 agent 类型筛选 (如 'hermes')，空=全部。
        limit: 返回的最大记录数。
    """
    engine = _get_engine()
    repo = engine.repository

    records = repo.list(limit=limit) if hasattr(repo, 'list') else []
    by_status: dict[str, int] = {}
    recent: list[dict] = []

    for imp in records:
        status_val = imp.status.value if hasattr(imp.status, 'value') else str(imp.status)
        agent_val = imp.metadata.get("agent", "") if hasattr(imp, 'metadata') else ""

        by_status[status_val] = by_status.get(status_val, 0) + 1

        if agent_filter and agent_filter not in agent_val:
            continue

        gates = []
        for gr in getattr(imp, 'gate_results', []):
            gates.append({
                "gate": gr.gate_name.value if hasattr(gr.gate_name, 'value') else str(gr.gate_name),
                "passed": gr.passed,
            })

        recent.append({
            "id": imp.id,
            "name": imp.name,
            "status": status_val,
            "agent": agent_val,
            "fail_gate": imp.fail_gate,
            "fail_reason": (imp.fail_reason or "")[:120],
            "gates": gates,
            "created_at": str(imp.created_at)[:19] if hasattr(imp, 'created_at') else "",
        })

    return {
        "total_records": len(records),
        "filter": agent_filter or "all",
        "by_status": by_status,
        "recent": recent[:limit],
    }


# ═══════════════════════════════════════════════════════════════
#  health_check
# ═══════════════════════════════════════════════════════════════


@mcp.tool()
def health_check() -> dict[str, Any]:
    """检查 agent-prod 引擎和仓库健康状态。"""
    try:
        engine = _get_engine()
        repo = engine.repository

        return {
            "status": "healthy",
            "engine": "QualityGateEngine",
            "repository": type(repo).__name__,
            "record_count": repo.count() if hasattr(repo, 'count') else "unknown",
            "gates": [
                "gate0_permission",
                "gate1_execution",
                "gate2_trace_integrity",
                "gate3_regression",
                "gate4_gray_release",
                "gate5_release_audit",
                "gate6_answer_quality",
                "gate7_execution_consistency",
            ],
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════


def main():
    """agent-prod MCP server 入口。"""
    mcp.run()


if __name__ == "__main__":
    main()
