#!/usr/bin/env python3
"""回填 Hermes 历史 session 到 agent-prod。

读取 state.db 中所有已结束的 session，构建 AgentTrace payload，
批量 POST 到 agent-prod /v1/agent/evaluate。
"""
import json
import sqlite3
import sys
import time
from urllib import request

AGENT_PROD_URL = "http://localhost:8765"
STATE_DB = "/root/.hermes/state.db"

def get_session_messages(db, session_id: str) -> list[dict]:
    """从 state.db 读取 session 的所有消息。"""
    cur = db.execute(
        "SELECT id, role, content, tool_calls, timestamp FROM messages "
        "WHERE session_id = ? ORDER BY timestamp",
        (session_id,),
    )
    messages = []
    for row in cur.fetchall():
        messages.append({
            "id": row[0],
            "role": row[1],
            "content": row[2] or "",
            "tool_calls": row[3],
            "created_at": row[4],  # column is 'timestamp' in state.db
        })
    return messages

def build_trace_payload(session: dict, messages: list[dict]) -> dict:
    """与 hermes_evaluator._build_trace_payload 同逻辑。"""
    session_id = str(session["id"])
    ended_at = session.get("ended_at", time.time())
    started_at = session.get("started_at", ended_at)

    input_tokens = session.get("input_tokens", 0) or 0
    output_tokens = session.get("output_tokens", 0) or 0
    tool_call_count = session.get("tool_call_count", 0) or 0
    api_call_count = session.get("api_call_count", 0) or 0
    source = session.get("source", "cli")
    end_reason = session.get("end_reason", "unknown")

    duration_ms = (ended_at - started_at) * 1000

    # Build decisions
    decisions = []
    idx = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tc_raw = msg.get("tool_calls")
        if isinstance(tc_raw, str):
            try:
                tc_raw = json.loads(tc_raw)
            except (json.JSONDecodeError, TypeError):
                tc_raw = []

        tool_calls = []
        if isinstance(tc_raw, list):
            for tc in tc_raw:
                tc_id = tc.get("id", "") or tc.get("tool_call_id", "")
                tc_fn = tc.get("function", {}) if isinstance(tc, dict) else {}
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

    # Distribute tokens
    n = len(decisions)
    if n > 0 and (input_tokens > 0 or output_tokens > 0):
        per_in = input_tokens // n
        per_out = output_tokens // n
        rem_in = input_tokens - per_in * n
        rem_out = output_tokens - per_out * n
        for i, d in enumerate(decisions):
            d["prompt_tokens"] = per_in + (1 if i < rem_in else 0)
            d["completion_tokens"] = per_out + (1 if i < rem_out else 0)

    # Final output
    final_output = {"final_response": ""}
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            final_output["final_response"] = (msg.get("content") or "")[:2000]
            break

    # Success rate
    total_turns = len(decisions)
    error_turns = sum(
        1 for msg in messages
        if msg.get("role") == "tool" and "error" in (msg.get("content") or "").lower()
    )
    success_rate = (total_turns - error_turns) / max(total_turns, 1)

    budget_tokens = max(input_tokens + output_tokens, 1)

    return {
        "agent": "hermes",
        "session_id": session_id,
        "output": final_output,
        "decisions": decisions,
        "current_metrics": {
            "latency_p95_ms": duration_ms,
            "success_rate": round(success_rate, 4),
            "error_rate": round(1 - success_rate, 4),
            "token_efficiency": 1.0,
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
        "human_approver": "",
        "policy_tags": ["production"] if source == "cli" else ["internal"],
        "budget_tokens": budget_tokens,
        "budget_time_ms": int(duration_ms) * 2 if duration_ms > 0 else 120000,
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


def main():
    db = sqlite3.connect(STATE_DB)
    db.row_factory = sqlite3.Row

    # 获取所有已结束 session
    cur = db.execute(
        "SELECT * FROM sessions WHERE ended_at IS NOT NULL ORDER BY ended_at"
    )
    sessions = cur.fetchall()
    total = len(sessions)
    print(f"共 {total} 个已结束 session，开始回填...")

    passed = 0
    failed = 0
    errors = 0
    results = {"production": 0, "rejected": 0, "error": 0}
    gate_stats = {}

    for i, row in enumerate(sessions):
        session = dict(row)
        sid = session["id"]

        try:
            messages = get_session_messages(db, sid)
            if not messages:
                continue

            payload = build_trace_payload(session, messages)
            data = json.dumps(payload).encode("utf-8")
            req = request.Request(
                f"{AGENT_PROD_URL}/v1/agent/evaluate",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            status = result.get("status", "?")
            results[status] = results.get(status, 0) + 1
            passed += 1

            # 收集门级统计
            for g in result.get("gates", []):
                gn = g.get("gate_name", g.get("gate", "?"))
                if gn not in gate_stats:
                    gate_stats[gn] = {"pass": 0, "fail": 0}
                if g.get("passed"):
                    gate_stats[gn]["pass"] += 1
                else:
                    gate_stats[gn]["fail"] += 1

            if i % 10 == 0 or i == total - 1:
                print(f"  [{i+1}/{total}] {sid[:30]}... status={status}")

        except Exception as e:
            failed += 1
            results["error"] = results.get("error", 0) + 1
            if i % 10 == 0 or i == total - 1:
                print(f"  [{i+1}/{total}] {sid[:30]}... ERROR: {e}")

        time.sleep(0.05)  # 温和限速

    db.close()

    print(f"\n回填完成: {passed}成功 {failed}失败")
    print(f"结果分布: {results}")
    print(f"\n各门通过率:")
    for gn in sorted(gate_stats.keys()):
        s = gate_stats[gn]
        rate = s["pass"] / max(s["pass"] + s["fail"], 1) * 100
        print(f"  {gn}: {s['pass']}P/{s['fail']}F ({rate:.0f}%)")


if __name__ == "__main__":
    main()
