# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""agent-prod Python SDK — 一行接入质量管道。

Usage:
    from agent_prod import trace

    # 一行接入
    result = trace(
        agent="my-agent",
        session_id="ses_001",
        decisions=[{
            "decision_id": "d1",
            "model": "gpt-4",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "tool_calls": [{
                "tool_id": "t1",
                "tool_name": "search",
                "arguments": {"query": "weather"},
                "result_summary": "Sunny, 22C",
                "success": True,
                "duration_ms": 120.0,
            }],
        }],
        current_metrics={
            "latency_p95_ms": 300,
            "success_rate": 0.99,
            "expected_answer": "Sunny, 22C",
            "final_response": "Sunny, 22C",
        },
        traffic_percentage=100,
        human_approver="auto",
    )

    if result["passed"]:
        deploy()
    else:
        alert(result["fail_reason"])

环境变量:
    AGENT_PROD_URL  — agent-prod 服务地址 (默认 http://localhost:8765)
    AGENT_PROD_V2   — 使用 /v2/ 端点 (默认 False — 使用 /v1/)
"""

from __future__ import annotations

import os
import time
import json
import logging
from typing import Any
from urllib import request as urllib_request
from urllib.error import URLError

logger = logging.getLogger(__name__)

AGENT_PROD_URL = os.environ.get("AGENT_PROD_URL", "http://localhost:8000")
USE_V2 = os.environ.get("AGENT_PROD_V2", "").lower() in ("1", "true", "yes")
EVALUATE_PATH = "/v2/agent/evaluate" if USE_V2 else "/v1/agent/evaluate"


def _send(payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """POST payload to agent-prod evaluate endpoint."""
    url = f"{AGENT_PROD_URL}{EVALUATE_PATH}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib_request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        raise ConnectionError(f"Cannot reach agent-prod at {url}: {e}") from e


def trace(
    agent: str = "generic",
    session_id: str = "",
    decisions: list[dict] | None = None,
    current_metrics: dict | None = None,
    baseline_metrics: dict | None = None,
    traffic_percentage: int = 0,
    human_approver: str = "",
    policy_tags: list[str] | None = None,
    declared_tools: list[str] | None = None,
    auth_grant_id: str = "",
    version: str = "",
    budget_tokens: int = 100_000,
    budget_time_ms: int = 120_000,
    metadata: dict | None = None,
    gate7_mode: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """发送一次 agent trace 到 agent-prod 管道评估。

    Args:
        gate7_mode: "observe"（默认，偏离不阻断）或 "enforce"（critical 偏离拒绝）。
                    未设置时使用 config.yaml 的 gate7.mode 配置。
    Returns:
        {
            "agent": str,
            "session_id": str,
            "status": "production" | "rejected",
            "passed": bool,
            "gates": [...],         # 每道门的详细结果
            "failed_at": str | None,
            "fail_reason": str | None,
            "total_duration_ms": float,
        }
    """
    import uuid

    sid = session_id or f"ses_{uuid.uuid4().hex[:12]}"
    meta = dict(metadata or {})
    if gate7_mode:
        meta["gate7_mode"] = gate7_mode
    payload: dict[str, Any] = {
        "agent": agent,
        "version": version,
        "session_id": sid,
        "decisions": decisions or [],
        "current_metrics": current_metrics or {},
        "baseline_metrics": baseline_metrics,
        "traffic_percentage": traffic_percentage,
        "human_approver": human_approver,
        "policy_tags": policy_tags or [],
        "declared_tools": declared_tools or [],
        "auth_grant_id": auth_grant_id,
        "budget_tokens": budget_tokens,
        "budget_time_ms": budget_time_ms,
        "metadata": meta,
    }
    return _send(payload, timeout=timeout)


def evaluate_batch(traces: list[dict], timeout: float = 60.0) -> list[dict]:
    """批量评估多条 trace（串行）。"""
    results = []
    for t in traces:
        try:
            r = _send(t, timeout=timeout / max(len(traces), 1))
            results.append(r)
        except Exception as e:
            results.append({"error": str(e), "session_id": t.get("session_id", "?")})
    return results


def health() -> dict[str, Any]:
    """健康检查。"""
    url = f"{AGENT_PROD_URL}/health"
    try:
        resp = urllib_request.urlopen(url, timeout=5.0)
        return json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        return {"status": "unreachable", "error": str(e)}


def quick(
    final_response: str,
    expected_answer: str,
    agent: str = "generic",
    **kwargs,
) -> dict[str, Any]:
    """快速评估：只传答案和期望，检测 Gate6。

    Example:
        >>> quick("巴黎是法国的", "巴黎是法国的")
        {'passed': True, 'status': 'production', 'gates': 7}
    """
    return trace(
        agent=agent,
        current_metrics={
            "latency_p95_ms": 100,
            "success_rate": 1.0,
            "expected_answer": expected_answer,
            "final_response": final_response,
        },
        traffic_percentage=1,
        human_approver="quick",
        **kwargs,
    )
