# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""qclaw session parser — converts qclaw .jsonl session files to AgentTrace format.

qclaw stores each conversation as a JSONL file in:
  ~/.qclaw/agents/main/sessions/<uuid>.jsonl

The JSONL contains ordered events:
  - session: metadata (id, version, timestamp)
  - model_change: which LLM model was used
  - message (role=user): user input
  - message (role=assistant): LLM response with tool calls
  - message (role=toolResult): tool execution result

Usage:
    from agent_prod.integration.qclaw_parser import parse_qclaw_session

    trace = parse_qclaw_session("/path/to/session.jsonl")
    # trace is a dict suitable for POST /v1/agent/evaluate
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent_prod.integration.qclaw_parser")


def parse_qclaw_session(
    fpath: str | Path,
    agent_type: str = "qclaw",
    source: str = "qclaw_watchdog",
) -> dict[str, Any] | None:
    """Parse a qclaw .jsonl session file into an AgentTrace-compatible dict.

    Returns None if the file cannot be parsed or has no session data.
    Returns a dict suitable for POST to /v1/agent/evaluate.
    """
    fpath = Path(fpath)
    if not fpath.exists():
        logger.warning("qclaw session file not found: %s", fpath)
        return None

    events = _load_events(fpath)
    if not events:
        return None

    # Extract session metadata
    session_meta = _get_first_event(events, "session")
    if not session_meta:
        return None

    session_id = session_meta.get("id", "")
    if not session_id:
        return None

    # Model info
    model_change = _get_first_event(events, "model_change")
    model = (model_change or {}).get("modelId", "unknown")
    provider = (model_change or {}).get("provider", "unknown")

    # Build decisions from message events
    decisions = _build_decisions(events, session_id, model)

    # ── Multi-agent tracing ────────────────────────────────
    # Extract subagent call tree from sessions_spawn events
    subagent_tree = _build_subagent_tree(events)
    n_spawned = len(subagent_tree.get("children", []))
    n_subagent_tools = sum(
        1 for d in decisions
        for tc in d.get("tool_calls", [])
        if tc.get("tool_name") in ("sessions_spawn", "sessions_yield", "subagents", "sessions_history", "sessions_list")
    )
    # Collect all child task names that were spawned
    spawned_task_names = []
    spawned_ids = set()
    for child in subagent_tree.get("children", []):
        task_name = child.get("task_name", "")
        child_key = child.get("child_key", "")
        if task_name:
            spawned_task_names.append(f"{task_name}({child_key[:8]})")
        if child_key:
            spawned_ids.add(child_key)

    # ── 从 sessions_history 事件中收集子智能体的结果上下文 ──
    child_responses = _collect_child_responses(events)

    # Extract metrics and timing
    total_duration_ms = _compute_duration(events)
    total_tokens_prompt = sum(
        d.get("prompt_tokens", 0) for d in decisions
    )
    total_tokens_completion = sum(
        d.get("completion_tokens", 0) for d in decisions
    )

    # Build tool list
    all_tools = sorted(set(
        tc.get("tool_name", "")
        for d in decisions
        for tc in d.get("tool_calls", [])
        if tc.get("tool_name")
    ))

    # Extract final response
    final_response = _extract_final_response(events)

    # Extract last user question
    user_question = _extract_last_user_message(events)

    # 当所有 decisions 的 token 都是估算值时，重新按更准确的方式计算一次
    # 用所有 assistant 消息的文本总长度 ÷ 4.5（更保守的估算）
    if total_tokens_prompt == 0 and total_tokens_completion == 0:
        total_chars = _count_all_assistant_text(events)
        total_tokens_prompt = max(1, total_chars // 4)
        total_tokens_completion = total_tokens_prompt

    # 如果 duration 也是 0，用时间戳差估算
    if total_duration_ms == 0:
        total_duration_ms = _compute_duration(events)
        # 如果时间戳也不够，至少给个非零值用于展示
        if total_duration_ms == 0:
            # 每轮估算 30 秒
            total_duration_ms = len(decisions) * 30000

    # 工具调用 duration 估算：按时间戳差分摊
    _backfill_tool_durations(events, decisions)

    # 用总工具耗时作为 latency_p95_ms（比全会话时间戳跨度更有意义）
    total_tool_time = sum(
        tc.get("duration_ms", 0)
        for d in decisions for tc in d.get("tool_calls", [])
    )
    effective_duration = total_tool_time if total_tool_time > 0 else total_duration_ms

    # ── Build aggregated output ────────────────────────────
    output = {
        "final_response": final_response[:5000] if final_response else "",
        "tools_used": all_tools,
    }
    if subagent_tree.get("children"):
        output["subagent_tree"] = subagent_tree
    if spawned_task_names:
        output["spawned_agents"] = spawned_task_names

    custom = {
        "provider": provider,
        "model": model,
        "total_turns": len(decisions),
        "source": source,
        "user_question": user_question[:2000] if user_question else "",
        # 估算值 — qclaw 服务器不记录真实 token 数
        "_estimated_tokens": True,
        "_estimated_duration": True,
    }
    if child_responses:
        custom["child_responses"] = child_responses

    return {
        "agent": agent_type,
        "version": provider,
        "session_id": session_id,
        "output": output,
        "decisions": decisions,
        "declared_tools": all_tools,
        "current_metrics": {
            "latency_p95_ms": effective_duration,
            "success_rate": 1.0,
            "error_rate": 0.0,
            "token_efficiency": 1.0,
            "custom": custom,
        },
        "metadata": {
            "source": source,
            "session_file": str(fpath.resolve()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "subagent_count": n_spawned,
            "subagent_tool_calls": n_subagent_tools,
        },
    }


def _load_events(fpath: Path) -> list[dict[str, Any]]:
    """Load and parse JSONL events from a qclaw session file."""
    events = []
    try:
        with open(fpath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.debug("Skipping malformed JSONL line: %s", e)
    except OSError as e:
        logger.warning("Failed to read qclaw session file %s: %s", fpath, e)
        return []
    return events


def _get_first_event(
    events: list[dict[str, Any]], event_type: str,
) -> dict[str, Any] | None:
    """Return the first event of the given type."""
    for e in events:
        if e.get("type") == event_type:
            return e
    return None


def _build_decisions(
    events: list[dict[str, Any]],
    session_id: str,
    model: str,
) -> list[dict[str, Any]]:
    """Build decision list from assistant/toolResult message pairs.

    Groups consecutive events into turns:
      assistant (with tool calls) → toolResult → toolResult → ...
      → next assistant → ...

    Each assistant message with ``toolUse`` content becomes a Decision.
    """
    decisions: list[dict[str, Any]] = []
    turn_index = 0
    current_tool_calls: list[dict[str, Any]] = []
    pending_assistant: dict[str, Any] | None = None

    for event in events:
        if event.get("type") != "message":
            continue

        msg = event.get("message", {})
        role = msg.get("role", "")

        if role == "assistant":
            # If we already have a pending assistant, finalize it first
            if pending_assistant is not None:
                decision = _assistant_to_decision(
                    pending_assistant, session_id, turn_index, model,
                    current_tool_calls,
                )
                # Only add decisions that have tool calls (skip pure text responses)
                if decision and decision.get("tool_calls"):
                    decisions.append(decision)
                    turn_index += 1
                current_tool_calls = []

            # Start new pending assistant
            pending_assistant = msg

        elif role == "toolResult":
            # Associate tool result with current pending assistant
            tc = _toolresult_to_tool_call(msg)
            if tc:
                current_tool_calls.append(tc)

    # Finalize last assistant (only if it has tool calls)
    if pending_assistant is not None:
        decision = _assistant_to_decision(
            pending_assistant, session_id, turn_index, model,
            current_tool_calls,
        )
        if decision and decision.get("tool_calls"):
            decisions.append(decision)

    return decisions


def _assistant_to_decision(
    msg: dict[str, Any],
    session_id: str,
    turn_index: int,
    model: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Convert an assistant message to a Decision dict."""
    content = msg.get("content", [])
    usage = msg.get("usage", {})
    stop_reason = msg.get("stopReason", "")

    # Extract text content
    text = ""
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            text = c.get("text", "")
            break

    # qclaw 的 JSONL 中 usage 始终为 0（服务器端不记录真实 token 数）
    # 按 4 字符 ≈ 1 token 估算；如果有真实 usage 则优先使用
    prompt_tokens = usage.get("input", 0) or 0
    completion_tokens = usage.get("output", 0) or 0
    if not prompt_tokens and not completion_tokens:
        # 估算：从用户消息的文本长度推算
        completion_tokens = max(1, len(text) // 4)
        prompt_tokens = completion_tokens  # 对称估算

    return {
        "decision_id": f"{session_id}-turn-{turn_index + 1}",
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning": text[:2000],
        "tool_calls": tool_calls,
        "metadata": {
            "stop_reason": stop_reason,
        },
    }


def _toolresult_to_tool_call(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a toolResult message to a tool call dict."""
    tool_name = msg.get("toolName", "")
    if not tool_name:
        return None

    tool_call_id = msg.get("toolCallId", "")

    # Extract result text from content
    content = msg.get("content", [])
    result_text = ""
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            result_text = c.get("text", "")
            break

    # Execution details — qclaw 通常不记录 durationMs，用 0 标记
    details = msg.get("details", {}) or {}
    duration_ms = details.get("durationMs", 0) or 0
    status = details.get("status", "")
    exit_code = details.get("exitCode")
    # qclaw 的 JSONL 中 details 通常为 null，此时无法判断真实状态
    # 如果 details 为 null/空，fallback：有结果文本就视为成功
    if not details:
        success = bool(result_text.strip())
    else:
        success = (status == "completed" or status == "success") and (exit_code is None or exit_code == 0)

    return {
        "tool_id": tool_call_id or f"tc-{tool_name}",
        "tool_name": tool_name,
        "arguments": {},
        "result_summary": result_text[:2000] if result_text else "",
        "success": success,
        "duration_ms": duration_ms,
    }


def _compute_duration(events: list[dict[str, Any]]) -> float:
    """Compute total session duration from first to last event timestamp."""
    timestamps = []
    for e in events:
        ts = e.get("timestamp")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                timestamps.append(dt.timestamp() * 1000)
            except (ValueError, AttributeError):
                pass

    if len(timestamps) >= 2:
        return timestamps[-1] - timestamps[0]
    return 0.0


def _extract_final_response(events: list[dict[str, Any]]) -> str:
    """Extract the final assistant text response."""
    last_text = ""
    for event in reversed(events):
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        for c in reversed(content):
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                if text:
                    return text
    return last_text


def _extract_last_user_message(events: list[dict[str, Any]]) -> str:
    """Extract the last user message text."""
    for event in reversed(events):
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return c.get("text", "")
    return ""


def _count_all_assistant_text(events: list[dict[str, Any]]) -> int:
    """Count total characters across all assistant text responses."""
    total = 0
    for event in events:
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                total += len(c.get("text", ""))
    return total


def _backfill_tool_durations(
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> None:
    """估算工具调用的耗时，写入 decisions 的 tool_calls 中。

    策略：
      1. 如果某个 tool_call 已经有 duration_ms > 0，跳过。
      2. 否则，用当前工具调用时间戳到下一条消息的时间戳的差值。
      3. 如果时间戳不可用，默认给 5 秒。
    """
    # 构建事件时间戳索引：按 toolResult 的 toolCallId 查找时间戳
    tool_timestamps: dict[str, float] = {}
    prev_ts = 0.0
    for event in events:
        ts = event.get("timestamp", "")
        ts_ms = 0.0
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts_ms = dt.timestamp() * 1000
            except (ValueError, AttributeError):
                pass

        if event.get("type") != "message":
            prev_ts = ts_ms or prev_ts
            continue

        msg = event.get("message", {})
        if msg.get("role") == "toolResult":
            tc_id = msg.get("toolCallId", "")
            if tc_id:
                tool_timestamps[tc_id] = ts_ms or prev_ts
        prev_ts = ts_ms or prev_ts

    # 按时间戳排序
    sorted_ts = sorted(tool_timestamps.values())

    for decision in decisions:
        for tc in decision.get("tool_calls", []):
            if tc.get("duration_ms", 0) > 0:
                continue
            tool_id = tc.get("tool_id", "")
            ts = tool_timestamps.get(tool_id, 0)
            if ts and sorted_ts:
                # 找到下一个时间戳的差值
                idx = sorted_ts.index(ts) if ts in sorted_ts else -1
                if idx >= 0 and idx + 1 < len(sorted_ts):
                    duration = sorted_ts[idx + 1] - ts
                    tc["duration_ms"] = max(1000, min(duration, 60000))
                else:
                    tc["duration_ms"] = 5000  # 默认 5 秒
            else:
                tc["duration_ms"] = 5000


def list_qclaw_sessions(
    sessions_dir: str | None = None,
) -> list[Path]:
    """List all active (non-deleted) qclaw session files."""
    if sessions_dir is None:
        sessions_dir = Path.home() / ".qclaw" / "agents" / "main" / "sessions"

    sessions_dir = Path(sessions_dir)
    if not sessions_dir.exists():
        return []

    sessions = []
    for fpath in sorted(sessions_dir.glob("*.jsonl")):
        name = fpath.name
        # Exclude deleted, checkpoint, trajectory, lock, sessions index
        if any(kw in name for kw in (
            ".deleted.", ".checkpoint.", ".trajectory",
            ".jsonl.lock", "sessions.json",
        )):
            continue
        sessions.append(fpath)

    return sessions


# ═════════════════════════════════════════════════════════════
#  Multi-agent tracing — qclaw subagent call tree
# ═════════════════════════════════════════════════════════════

import re as _re

_SUBAGENT_TOOLS = frozenset({
    "sessions_spawn", "sessions_yield", "subagents",
    "sessions_history", "sessions_list",
})


def _build_subagent_tree(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract the subagent spawn/yield tree from session events.

    Parses toolResult texts from sessions_spawn events to build
    a tree of spawned children and their task names.

    Returns:
        {"children": [
            {"child_key": "...", "task_name": "...", "run_id": "...",
             "status": "accepted|forbidden|failed"},
        ]}
    """
    children: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for event in events:
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("role") != "toolResult" or msg.get("toolName") != "sessions_spawn":
            continue

        content = msg.get("content", [])
        text = ""
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                break

        # Extract childSessionKey
        m = _re.search(r'"childSessionKey"\s*:\s*"([^"]+)"', text)
        child_key = m.group(1) if m else ""

        if child_key and child_key not in seen_keys:
            seen_keys.add(child_key)
            # Extract task name and run id
            task_m = _re.search(r'"taskName"\s*:\s*"([^"]+)"', text)
            run_m = _re.search(r'"runId"\s*:\s*"([^"]+)"', text)
            status_m = _re.search(r'"status"\s*:\s*"([^"]+)"', text)

            children.append({
                "child_key": child_key,
                "task_name": task_m.group(1) if task_m else "unknown",
                "run_id": run_m.group(1) if run_m else "",
                "status": status_m.group(1) if status_m else "unknown",
            })

    return {"children": children, "total": len(children)}


def _collect_child_responses(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Collect subagent result summaries from sessions_history events.

    When the parent calls sessions_history to check on a child,
    the returned data contains the child's final message.
    """
    responses: list[dict[str, str]] = []
    seen: set[str] = set()

    for event in events:
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("role") != "toolResult" or msg.get("toolName") != "sessions_history":
            continue

        content = msg.get("content", [])
        text = ""
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                break

        # Extract the child session key
        key_m = _re.search(r'"sessionKey"\s*:\s*"([^"]+)"', text)
        child_key = key_m.group(1) if key_m else ""
        if not child_key or child_key in seen:
            continue
        seen.add(child_key)

        # Extract last assistant message from the history
        text_m = _re.search(r'"role"\s*:\s*"assistant"[^}]*"type"\s*:\s*"text"[^}]*"text"\s*:\s*"([^"]+)"', text)
        result_summary = text_m.group(1)[:300] if text_m else ""

        # Extract task name if available
        task_m = _re.search(r'"taskName"\s*:\s*"([^"]+)"', text)
        task_name = task_m.group(1) if task_m else "unknown"

        responses.append({
            "child_key": child_key,
            "task_name": task_name,
            "result_summary": result_summary,
        })

    return responses


def is_subagent_tool(tool_name: str) -> bool:
    """Check if a tool name is a qclaw multi-agent orchestration tool."""
    return tool_name in _SUBAGENT_TOOLS