"""一次性批量校准：把 125 个真实 Hermes session 送入质量门，收集指标统计。

Usage:
    python3 tools/calibrate_thresholds.py
    python3 tools/calibrate_thresholds.py --limit 20 --url http://localhost:8000
"""

import json
import time
import sys
from pathlib import Path
from collections import defaultdict

SESSION_DIR = Path.home() / ".hermes" / "sessions"

def parse_session(filepath: Path) -> dict:
    """Convert a Hermes session file to AgentTrace payload."""
    with open(filepath) as f:
        sess = json.load(f)

    sid = sess.get("session_id", "")
    messages = sess.get("messages", [])
    model = sess.get("model", "unknown")
    msg_count = sess.get("message_count", len(messages))

    # Count tokens by role
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages if m.get("role") in ("user", "system"))
    resp_chars = sum(len(str(m.get("content", ""))) for m in messages if m.get("role") == "assistant")
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, resp_chars // 4)

    # Tool definitions (from session, not actual invocations per-turn)
    tools = sess.get("tools", [])
    tool_calls = []
    for t in tools:
        name = t.get("name", str(t)) if isinstance(t, dict) else str(t)
        tool_calls.append({"tool_id": name, "tool_name": name, "arguments": {}, "success": True, "duration_ms": 0})

    # Duration
    try:
        from datetime import datetime
        s = datetime.fromisoformat(sess.get("session_start", ""))
        e = datetime.fromisoformat(sess.get("last_updated", ""))
        dur = (e - s).total_seconds() * 1000 if e > s else 0
    except Exception:
        dur = 0

    # Agent type
    agent_type = "hermes"
    if "claude" in model.lower():
        agent_type = "claude-code"
    elif "gpt" in model.lower() or "openai" in model.lower():
        agent_type = "codex"

    return {
        "session_id": sid,
        "agent": agent_type,
        "model": model,
        "output": _last_assistant(messages)[:2000],
        "decisions": [{
            "decision_id": f"{sid}-turn-1",
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "output": _last_assistant(messages)[:2000],
            "tool_calls": tool_calls,
        }],
        "current_metrics": {
            "tokens_total": prompt_tokens + completion_tokens,
            "duration_ms": dur,
            "turns": max(1, msg_count // 2),
            "tool_calls": len(tool_calls),
            "f1_score": 0.95,
        },
        "baseline_metrics": {},
        "traffic_metrics": {},
        "policy_tags": [],
        "metadata": {"source": "batch_calibration", "file": str(filepath)},
    }

def _last_assistant(messages):
    for m in reversed(messages):
        if m.get("role") == "assistant":
            return str(m.get("content", ""))
    return ""

def submit(url, payload):
    import urllib.request
    import urllib.error
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"status": "error", "error": f"HTTP {e.code}: {body[:200]}", "passed": False}
    except Exception as e:
        return {"status": "error", "error": str(e), "passed": False}

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--delay", type=float, default=0.1)
    args = p.parse_args()

    files = sorted(SESSION_DIR.glob("session_*.json"))
    if args.limit:
        files = files[:args.limit]

    print(f"🔬 Calibration: {len(files)} sessions → {args.url}/v1/agent/evaluate")
    print()

    stats = {
        "total": 0, "passed": 0, "rejected": 0, "gray": 0, "error": 0,
        "gate_failures": defaultdict(int),
        "durations_ms": [],
        "tokens": [],
        "models": defaultdict(int),
        "agents": defaultdict(int),
    }

    results = []

    for i, fpath in enumerate(files):
        try:
            payload = parse_session(fpath)
        except Exception:
            continue

        result = submit(f"{args.url}/v1/agent/evaluate", payload)
        stats["total"] += 1
        status = result.get("status", "error")
        passed = result.get("passed", False)

        if status == "production":
            stats["passed"] += 1
        elif status == "gray":
            stats["gray"] += 1
        elif status == "rejected":
            stats["rejected"] += 1
            fail_gate = result.get("failed_at", "unknown")
            stats["gate_failures"][fail_gate] += 1
        else:
            stats["error"] += 1

        stats["durations_ms"].append(result.get("total_duration_ms", 0))
        dur_ms = payload["current_metrics"]["duration_ms"]
        if dur_ms > 0:
            stats["durations_ms"].append(dur_ms)

        tok = payload["current_metrics"]["tokens_total"]
        stats["tokens"].append(tok)
        stats["models"][payload["model"]] += 1
        stats["agents"][payload["agent"]] += 1

        results.append({
            "session_id": payload["session_id"],
            "status": status,
            "passed": passed,
            "tokens": tok,
            "duration_ms": dur_ms,
            "model": payload["model"],
        })

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(files)}] {stats['passed']} passed, {stats['rejected']} rejected, {stats['error']} errors")

        time.sleep(args.delay)

    # ── Report ──
    print(f"\n{'='*60}")
    print(f"📊 CALIBRATION REPORT")
    print(f"{'='*60}")
    print(f"Total sessions:    {stats['total']}")
    print(f"  ✅ Passed:       {stats['passed']} ({stats['passed']/max(1,stats['total'])*100:.1f}%)")
    print(f"  ⚠️  Gray:        {stats['gray']} ({stats['gray']/max(1,stats['total'])*100:.1f}%)")
    print(f"  ❌ Rejected:     {stats['rejected']} ({stats['rejected']/max(1,stats['total'])*100:.1f}%)")
    print(f"  💥 Error:        {stats['error']}")
    print()

    if stats["gate_failures"]:
        print("Gate failures:")
        for gate, count in sorted(stats["gate_failures"].items()):
            print(f"  {gate}: {count}")

    if stats["durations_ms"]:
        d = sorted(stats["durations_ms"])
        n = len(d)
        print(f"\nSession duration (ms):")
        print(f"  min={d[0]:.0f}  P50={pctl(d,50):.0f}  P90={pctl(d,90):.0f}  P95={pctl(d,95):.0f}  P99={pctl(d,99):.0f}  max={d[-1]:.0f}")

    if stats["tokens"]:
        t = sorted(stats["tokens"])
        n = len(t)
        print(f"\nToken usage:")
        print(f"  min={t[0]}  P50={pctl(t,50):.0f}  P90={pctl(t,90):.0f}  P95={pctl(t,95):.0f}  P99={pctl(t,99):.0f}  max={t[-1]}")

    print(f"\nModels:")
    for m, c in sorted(stats["models"].items(), key=lambda x: -x[1]):
        print(f"  {m}: {c}")

    print(f"\nAgents:")
    for a, c in sorted(stats["agents"].items(), key=lambda x: -x[1]):
        print(f"  {a}: {c}")

    # ── Threshold recommendations ──
    print(f"\n{'='*60}")
    print("🎯 RECOMMENDED THRESHOLDS (P95-based)")
    print(f"{'='*60}")
    if len(t) >= 10:
        print(f"  token_budget:    {pctl(t, 95):.0f} tokens (P95 of real data)")
        print(f"  time_budget_ms:  {pctl(d, 95):.0f} ms (P95 of real data)")
    print(f"  regress_pct:     0.93 (standard — tighten after more data)")
    print(f"  error_rate_max:  0.05")
    print(f"  pass_rate:       {stats['passed']/max(1,stats['total'])*100:.1f}% overall")

def pctl(sorted_list, p):
    """Percentile from sorted list."""
    if not sorted_list:
        return 0
    idx = int(len(sorted_list) * p / 100.0)
    idx = max(0, min(len(sorted_list)-1, idx))
    return sorted_list[idx]

if __name__ == "__main__":
    main()
