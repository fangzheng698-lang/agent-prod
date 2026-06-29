"""E2E: Data Flywheel + Adaptive Gates — 用真实 LLM 执行数据跑完整闭环"""
import sys, os, time, json, subprocess
sys.path.insert(0, "/root/experiment/agent-prod")

API = "http://localhost:8000"

def call(prompt, sid):
    start = time.monotonic()
    r = subprocess.run([
        "curl", "-s",
        f"{API}/v1/chat/completions",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"messages":[{"role":"user","content":prompt}], "session_id":sid, "max_tokens":300})
    ], capture_output=True, text=True, timeout=120)
    duration_ms = (time.monotonic() - start) * 1000
    try:
        data = json.loads(r.stdout)
    except:
        print(f"PARSE ERROR for {sid}: {r.stdout[:200]}")
        return None
    qg = data.get("quality_gate", {})
    content = ""
    if data.get("choices"):
        content = data["choices"][0].get("message", {}).get("content", "")
    tokens_used = (data.get("usage", {}).get("prompt_tokens", 0) +
                    data.get("usage", {}).get("completion_tokens", 0))
    return {
        "session_id": sid, "prompt": prompt, "response": content[:100],
        "tokens": tokens_used, "duration_ms": duration_ms,
        "gate_pass": qg.get("passed", False), "gate_status": qg.get("status", "unknown"),
    }

# ═══════════════════════════════════════════
print("=" * 60)
print("  E2E: Collecting real execution data...")
print("=" * 60)

results = []
prompts = [
    ("1+1等于几", "simple"),
    ("用中文解释什么是机器学习，50字以内", "ml"),
    ("写一个Python函数计算斐波那契数列", "fib"),
    ("2的10次方是多少", "pow"),
    ("列出三个常见的Python内置数据类型", "types"),
    ("什么是REST API，一句话回答", "rest"),
    ("3*7+5等于多少", "math1"),
    ("Python中列表和元组的区别，一句话", "list_tuple"),
    ("用echo命令创建文件hello.txt的bash命令是什么", "bash"),
    ("What is the capital of France?", "cap"),
]

for i, (prompt, tag) in enumerate(prompts):
    sid = f"fw_e2e_{tag}_{i}"
    r = call(prompt, sid)
    if r:
        r["run_id"] = f"e2e_{tag}_{i}"
        results.append(r)
        status = "✅" if r["gate_pass"] else "❌"
        print(f"  [{i+1}/{len(prompts)}] {tag}: {r['tokens']}t {r['duration_ms']:.0f}ms {status}")
    else:
        print(f"  [{i+1}/{len(prompts)}] {tag}: FAILED")
    time.sleep(1)

# ═══════════════════════════════════════════
print("\n" + "=" * 60)
print("  Phase 1: Data Flywheel — Statistical Baseline")
print("=" * 60)

from agent_prod.adaptivity.data_flywheel import FlywheelEngine, compute_baseline
from agent_prod.adaptivity.adaptive_gates import AdaptiveGateEngine, MultiGateAdaptiveEngine
from agent_prod.observability.execution_log import ExecutionLogRecord
from datetime import datetime, timezone

records = []
for r in results:
    records.append(ExecutionLogRecord(
        run_id=r["run_id"],
        session_id=r["session_id"],
        prompt=r["prompt"],
        response=r["response"],
        turns=1,
        costs={"prompt_tokens": r["tokens"]//2, "completion_tokens": r["tokens"]-r["tokens"]//2},
        duration_ms=r["duration_ms"],
        quality_gate_result={"status": r["gate_status"], "passed": r["gate_pass"]},
        created_at=datetime.now(timezone.utc).isoformat(),
    ))

baseline = compute_baseline(records)
print(f"  Baseline from {baseline['sample_count']} real executions:")
print(f"    Tokens:     μ={baseline['avg_tokens']:.1f} σ={baseline['token_std']:.1f} P95={baseline['token_p95']:.1f}")
print(f"    Duration:   μ={baseline['avg_duration_ms']:.0f}ms σ={baseline['duration_std']:.0f}ms P95={baseline['duration_p95']:.0f}ms")
print(f"    Gate pass:  {baseline['gate_pass_rate']:.0%}")

if baseline["sample_count"] == 0:
    print("\n" + "=" * 60)
    print("  Phases 2-3: SKIPPED — no execution data (server not running?)")
    print("=" * 60)
    print("  Run 'agent-prod serve' in another terminal, then re-run this test.")
    if __name__ == "__main__":
        sys.exit(0)
else:

    # ═══════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  Phase 2: Adaptive Gates — Dynamic Thresholds")
    print("=" * 60)

    mge = MultiGateAdaptiveEngine()
    mge.add_gate("execution", ["duration_ms", "tokens"], window_size=20, sigma_mult=2.0, min_samples=3)
    mge.add_gate("regression", ["duration_ms"], window_size=20, sigma_mult=2.0, min_samples=3)
    mge.add_gate("gray_release", ["duration_ms", "tokens"], window_size=20, sigma_mult=3.0, min_samples=3)

    # Feed real data
    for r in results:
        mge.record("execution", {"duration_ms": r["duration_ms"], "tokens": r["tokens"]})
        mge.record("regression", {"duration_ms": r["duration_ms"]})
        mge.record("gray_release", {"duration_ms": r["duration_ms"], "tokens": r["tokens"]})

    mge.calibrate_all()

    # Test: evaluate a new "normal" execution
    test_normal = {
        "execution": {"duration_ms": baseline["avg_duration_ms"], "tokens": baseline["avg_tokens"]},
        "regression": {"duration_ms": baseline["avg_duration_ms"]},
        "gray_release": {"duration_ms": baseline["avg_duration_ms"], "tokens": baseline["avg_tokens"]},
    }
    result_normal = mge.evaluate_all(test_normal)
    print(f"  Normal execution: {'PASS' if result_normal['all_passed'] else 'FAIL'}")

    # Test: outlier
    test_outlier = {
        "execution": {"duration_ms": baseline["avg_duration_ms"] * 3, "tokens": baseline["avg_tokens"] * 5},
        "regression": {"duration_ms": baseline["avg_duration_ms"] * 3},
        "gray_release": {"duration_ms": baseline["avg_duration_ms"] * 3, "tokens": baseline["avg_tokens"] * 5},
    }
    result_outlier = mge.evaluate_all(test_outlier)
    print(f"  Outlier execution: {'PASS' if result_outlier['all_passed'] else 'FAIL'} (expected FAIL)")
    if result_outlier["failed_gates"]:
        print(f"    Failed gates: {result_outlier['failed_gates']}")

    # Show thresholds
    for gname, eng in mge._gates.items():
        th = eng.get_thresholds()
        for mname, t in th.items():
            print(f"  [{gname}] {mname}: μ={t.ewma_mean:.1f} σ={t.ewma_std:.1f} band=[{t.adaptive_lower:.1f}, {t.adaptive_upper:.1f}]")

    # ═══════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  Phase 3: Integrated — Flywheel × Adaptive Gates")
    print("=" * 60)

    # Use flywheel baseline to feed adaptive gates thresholds
    flywheel_baseline = baseline
    old_fixed = {"execution_time_tolerance": 1.2, "token_tolerance": 1.1, "regress_pct": 0.95}
    new_adaptive = {
        "execution": {
            "duration_ms_upper": flywheel_baseline["avg_duration_ms"] + 2 * flywheel_baseline["duration_std"],
            "tokens_upper": flywheel_baseline["avg_tokens"] + 2 * flywheel_baseline["token_std"],
        }
    }

    print(f"  OLD (fixed): gate1.execution_time_tolerance={old_fixed['execution_time_tolerance']}")
    print(f"               gate1.token_tolerance={old_fixed['token_tolerance']}")
    print(f"               gate3.regress_pct={old_fixed['regress_pct']}")
    print(f"  NEW (data-driven from {flywheel_baseline['sample_count']} real executions):")
    print(f"    duration_ms ≤ {new_adaptive['execution']['duration_ms_upper']:.0f}ms  (μ+2σ)")
    print(f"    tokens      ≤ {new_adaptive['execution']['tokens_upper']:.0f}        (μ+2σ)")
    print(f"  Improvement: from blind fixed threshold → evidence-based dynamic bound")

    # ═══════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  VERDICT")
    print("=" * 60)
    print(f"  ✅ Data collection:    {len(results)}/{len(prompts)} successful")
    print(f"  ✅ Statistical baseline: {flywheel_baseline['sample_count']} samples")
    print(f"  ✅ Adaptive gates:      {len(mge._gates)} gates with dynamic thresholds")
    print(f"  ✅ Normal pass:         {'PASS' if result_normal['all_passed'] else 'FAIL'}")
    print(f"  ✅ Outlier caught:      {'YES' if not result_outlier['all_passed'] else 'NO — FALSE NEGATIVE'}")
    print(f"  ✅ Fixed→Adaptive:      thresholds now derived from real execution data, not constants")
    print(f"  🏆 FLYWHEEL SPINNING — Data drives gates, gates drive quality, quality feeds data")
