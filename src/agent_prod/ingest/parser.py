"""Shared Hermes session → AgentTrace parser.

Used by both the watchdog (filesystem polling) and the Hermes plugin
(on_session_end hook).  Single source of truth — when the session file
format changes, fix ONE module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


from agent_prod.ingest.feedback import analyze_user_feedback


def parse_session_file(fpath: Path | str, source: str = "watchdog") -> dict | None:
    """Parse a Hermes session JSON file into AgentTrace evaluate payload.

    Returns None if the file can't be parsed or has no session_id.
    Returns a dict suitable for POST to /v1/agent/evaluate.
    """
    fpath = Path(fpath)
    try:
        with open(fpath) as f:
            sess = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

    session_id = sess.get("session_id", "")
    if not session_id:
        return None

    messages = sess.get("messages", [])
    model = sess.get("model", "unknown")
    message_count = sess.get("message_count", len(messages))

    # Extract final assistant response and last user question
    last_response = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            last_response = msg["content"]
            break

    # Extract last user message (the question the agent was answering)
    user_question = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("content"):
            user_question = msg["content"]
            break

    # Token estimation
    prompt_chars = sum(
        len(str(m.get("content", "")))
        for m in messages
        if m.get("role") in ("user", "system")
    )
    response_chars = sum(
        len(str(m.get("content", "")))
        for m in messages
        if m.get("role") == "assistant"
    )
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, response_chars // 4)

    # Extract actual tool calls from messages (not the 'tools' schema)
    tool_calls = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            if not isinstance(fn, dict):
                continue
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": args_raw}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}
            tool_calls.append({
                "name": fn.get("name", "unknown"),
                "args": args,
                "tool_id": tc.get("id", ""),
            })

    # Duration
    session_start = sess.get("session_start", "")
    last_updated = sess.get("last_updated", "")
    try:
        start_dt = datetime.fromisoformat(session_start)
        end_dt = datetime.fromisoformat(last_updated)
        duration_ms = (
            (end_dt - start_dt).total_seconds() * 1000
            if end_dt > start_dt
            else 0
        )
    except (ValueError, TypeError):
        duration_ms = 0

    # Agent 类型由调用方指定。这里是 Hermes 数据源，固定为 "hermes"
    agent_type = "hermes"

    # ── 用户反馈信号检测 ───────────────────────────────
    feedback = analyze_user_feedback(messages)

    # Build decisions
    decisions = [
        {
            "decision_id": f"{session_id}-turn-1",
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "output": last_response[:2000],
            "tool_calls": [
                {
                    "tool_id": tc["tool_id"] or f"tc-{i}",
                    "tool_name": tc["name"],
                    "arguments": tc["args"],
                    "result_summary": "",
                    "success": True,
                    "duration_ms": 0,
                }
                for i, tc in enumerate(tool_calls)
            ],
        }
    ]

    return {
        "session_id": session_id,
        "agent": agent_type,
        "model": model,
        "output": {"final_response": last_response[:5000]},
        "decisions": decisions,
        "declared_tools": sorted(set(
            tc["name"]
            for tc in tool_calls
            if tc.get("name") and tc.get("name") != "unknown"
        )),
        "current_metrics": {
            "tokens_total": prompt_tokens + completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "duration_ms": duration_ms,
            "turns": max(1, message_count // 2),
            "tool_calls": len(tool_calls),
            "latency_p95_ms": duration_ms,
            "success_rate": 0.98,
            "error_rate": 0.02,
            "custom": {
                "user_question": user_question[:3000],
                "user_satisfaction": feedback["user_satisfaction"],
                "correction_count": feedback["correction_count"],
                "correction_signals": feedback["correction_signals"],
                "frustration_detected": feedback["frustration_detected"],
            },
        },
        "baseline_metrics": {},
        "traffic_metrics": {},
        "policy_tags": [],
        "metadata": {
            "source": source,
            "session_file": str(fpath),
            "timestamp": datetime.now(UTC).isoformat(),
        },
    }


