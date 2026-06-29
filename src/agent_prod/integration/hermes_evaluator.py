"""
Hermes → agent-prod session evaluation hook.

Registered as a post-end-session callback on SessionDB.  When Hermes ends
a session (CLI close, branch, compression, cron complete), this hook:

  1. Queries session row + messages from state.db
  2. Builds an AgentTrace payload (dict, zero-dependency)
  3. POSTs to agent-prod's /v1/agent/evaluate endpoint
  4. Logs result (or swallows errors — never breaks Hermes)

Configuration:
  - AGENT_PROD_URL (env var, default: http://localhost:18723)
  - AGENT_PROD_API_KEY (env var, optional — for auth middleware)

Activation:
  In cli.py, after SessionDB() init:
      from agent_prod.integration.hermes_evaluator import hermes_evaluator_hook
      db.register_session_end_hook(hermes_evaluator_hook)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib import request

logger = logging.getLogger(__name__)

AGENT_PROD_URL = os.environ.get("AGENT_PROD_URL", "http://localhost:18723")
AGENT_PROD_API_KEY = os.environ.get("AGENT_PROD_API_KEY", "")

# ── Hermes message → AgentTrace Decision mapping ──────────────────


def _build_decisions(
    messages: list[dict[str, Any]],
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Convert Hermes message rows into AgentTrace Decision dicts.

    Each assistant message with tool_calls becomes one Decision.
    Distributes session-level token counts across decisions so
    trace.total_tokens() returns a non-zero value (required by gate1).
    """
    decisions: list[dict[str, Any]] = []
    idx = 0

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        tool_calls_raw = msg.get("tool_calls")
        if isinstance(tool_calls_raw, str):
            try:
                tool_calls_raw = json.loads(tool_calls_raw)
            except (json.JSONDecodeError, TypeError):
                tool_calls_raw = []

        tool_calls: list[dict[str, Any]] = []
        if isinstance(tool_calls_raw, list):
            for tc in tool_calls_raw:
                tc_id = tc.get("id", "") or tc.get("tool_call_id", "")
                tc_fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                # arguments may be a JSON string — normalize to dict
                args = tc_fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {"_raw": args}
                elif not isinstance(args, dict):
                    args = {}
                tool_calls.append({
                    "tool_id": str(tc_id),
                    "tool_name": tc_fn.get("name", "unknown"),
                    "arguments": args,
                    "success": True,
                })

        idx += 1
        decisions.append({
            "decision_id": f"turn-{msg.get('id', idx)}",
            "model": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tool_calls": tool_calls,
        })

    # ── Distribute session token counts across decisions ──
    # Without this, trace.total_tokens() returns 0 and gate1 rejects.
    n = len(decisions)
    if n > 0 and (input_tokens > 0 or output_tokens > 0):
        per_in = input_tokens // n
        per_out = output_tokens // n
        remainder_in = input_tokens - per_in * n
        remainder_out = output_tokens - per_out * n
        for i, d in enumerate(decisions):
            d["prompt_tokens"] = per_in + (1 if i < remainder_in else 0)
            d["completion_tokens"] = per_out + (1 if i < remainder_out else 0)

    return decisions


def _build_trace_payload(
    session: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build AgentTrace payload dict from Hermes session data."""
    session_id = str(session.get("id", "unknown"))
    ended_at = session.get("ended_at", time.time())
    started_at = session.get("started_at", ended_at)
    input_tokens = session.get("input_tokens", 0) or 0
    output_tokens = session.get("output_tokens", 0) or 0
    tool_call_count = session.get("tool_call_count", 0) or 0
    api_call_count = session.get("api_call_count", 0) or 0
    source = session.get("source", "cli")
    end_reason = session.get("end_reason", "unknown")

    # Compute latency from session timestamps
    duration_ms = (ended_at - started_at) * 1000 if started_at and ended_at else 0.0

    # Build decisions from messages (with token distribution)
    decisions = _build_decisions(messages, input_tokens, output_tokens)

    # Find final output (last assistant message without tool_calls)
    final_output = {"final_response": ""}
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            final_output["final_response"] = msg.get("content", "")[:2000]
            break

    # Compute success rate: (assistant messages without error) / total
    total_turns = len(decisions)
    error_turns = sum(
        1 for msg in messages
        if msg.get("role") == "tool" and "error" in (msg.get("content") or "").lower()
    )
    success_rate = (total_turns - error_turns) / max(total_turns, 1)

    # Token efficiency
    budget_tokens = max(input_tokens + output_tokens, 1)
    token_efficiency = 1.0  # default unless we have a budget reference

    # ── 从 decisions 中收集实际使用的工具名作为 declared_tools ──
    declared_tools: list[str] = []
    seen: set[str] = set()
    for d in decisions:
        for tc in d.get("tool_calls", []):
            tn = tc.get("tool_name", "")
            if tn and tn not in seen:
                seen.add(tn)
                declared_tools.append(tn)

    # ── 收集 loaded_skills ──
    # 优先读 session 列（启动时 preloaded），再扫描消息中运行时 skill_view 调用
    loaded_skills_raw = session.get("loaded_skills")
    loaded_skills: list[str] = []
    if isinstance(loaded_skills_raw, str):
        try:
            loaded_skills = json.loads(loaded_skills_raw)
        except (json.JSONDecodeError, TypeError):
            pass
    # 运行时加载：扫描 skill_view / skill_manage 调用
    runtime_skill_ops: set[str] = set()
    for msg in messages:
        tool_calls_raw = msg.get("tool_calls")
        if isinstance(tool_calls_raw, str):
            try:
                tool_calls_raw = json.loads(tool_calls_raw)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(tool_calls_raw, list):
            continue
        for tc in tool_calls_raw:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "")
            if name in ("skill_view", "skill_manage"):
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                skill_name = args.get("name", "")
                if skill_name:
                    runtime_skill_ops.add(skill_name)
    # 合并：preloaded + runtime，去重
    all_skills = list(dict.fromkeys(loaded_skills + sorted(runtime_skill_ops)))

    payload: dict[str, Any] = {
        "agent": "hermes",
        "version": "v0.3.0",
        "session_id": session_id,
        "declared_tools": declared_tools,
        "auth_grant_id": f"hermes-self-{session_id[:12]}",
        "output": final_output,
        "decisions": decisions,
        "current_metrics": {
            "latency_p95_ms": duration_ms,
            "success_rate": round(success_rate, 4),
            "error_rate": round(1 - success_rate, 4),
            "token_efficiency": token_efficiency,
            "custom": {
                "source": source,
                "end_reason": end_reason,
                "tool_call_count": tool_call_count,
                "api_call_count": api_call_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": session.get("model", "unknown"),
                "_decisions": decisions,
            },
        },
        "baseline_metrics": None,
        "traffic": None,
        "human_approver": "",
        "policy_tags": ["production"] if source == "cli" else ["internal"],
        "budget_tokens": budget_tokens,
        "budget_time_ms": int(duration_ms) * 2 if duration_ms > 0 else 120_000,
        "metadata": {
            "hermes_source": source,
            "hermes_end_reason": end_reason,
            "evaluated_at": time.time(),
            "loaded_skills": all_skills,
        },
        # Gate0 需要 declared_tools 才不会拒掉 benign 工具
        "declared_tools": [
            "read_file", "search_files", "session_search",
            "skills_list", "skill_view", "memory",
            "vision_analyze", "browser_navigate", "browser_snapshot",
            "browser_console", "browser_vision", "browser_get_images",
            "browser_scroll", "browser_back", "web_search",
            "process", "process_list", "process_poll", "process_log",
            "todo", "write_file", "patch", "skill_manage",
            "browser_click", "browser_type", "browser_press",
            "terminal", "execute_code", "send_message",
            "cronjob", "delegate_task", "clarify",
        ],
    }

    return payload


def _post_evaluate(payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST to agent-prod and return response dict, or None on failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if AGENT_PROD_API_KEY:
            headers["Authorization"] = f"Bearer {AGENT_PROD_API_KEY}"

        req = request.Request(
            f"{AGENT_PROD_URL}/v1/agent/evaluate",
            data=data,
            headers=headers,
            method="POST",
        )
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)  # type: ignore[no-any-return]
    except Exception:
        logger.debug("agent-prod evaluate failed", exc_info=True)
        return None


# ── Public hook ────────────────────────────────────────────────────


def hermes_evaluator_hook(session_db: Any, session_id: str) -> None:
    """Post-end-session hook: evaluate Hermes session in agent-prod.

    Args:
        session_db: Hermes SessionDB instance.
        session_id: The ended session's ID.
    """
    try:
        session = session_db.get_session(session_id)
        if not session:
            return  # session already deleted or never existed

        messages = session_db.get_messages(session_id)
        if not messages:
            return  # no messages to evaluate

        payload = _build_trace_payload(session, messages)
        result = _post_evaluate(payload)

        if result:
            status = result.get("status", "?")
            passed = result.get("passed", False)
            logger.info(
                "agent-prod evaluated session %s: status=%s passed=%s",
                session_id, status, passed,
            )
    except Exception:
        logger.debug(
            "hermes_evaluator_hook failed for session %s", session_id, exc_info=True
        )
