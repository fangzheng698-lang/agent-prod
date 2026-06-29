#!/usr/bin/env python3
"""Phase 1 stress test: bulk submit 156 real sessions to /v1/agent/evaluate
   at varying concurrency levels and measure latency distribution."""

import sys, json, time, asyncio
from pathlib import Path
import urllib.request

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent_prod.ingest.watchdog import SessionWatchdog

URL = "http://localhost:8765"
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
CONCURRENCY_LEVELS = [1, 10, 50, 100]  # start small

async def submit_one(session_path, wd, idx):
    trace = wd._parse_session(session_path)
    if not trace:
        return None
    t0 = time.monotonic()
    try:
        result = wd._submit_evaluate(trace)
        elapsed = (time.monotonic() - t0) * 1000
        return {"session_id": trace["session_id"], "status": result.get("status"), "passed": result.get("passed"), "latency_ms": elapsed, "gates": {g["gate"]: g["passed"] for g in result.get("gates", [])}}
    except Exception as e:
        return {"session_id": trace.get("session_id", "?"), "status": "error", "passed": False, "latency_ms": (time.monotonic() - t0) * 1000, "error": str(e)[:100]}

async def stress_test(concurrency):
    sessions = sorted(SESSIONS_DIR.glob("session_*.json"))
    wd = SessionWatchdog(sessions_dir=SESSIONS_DIR, agent_prod_url=URL)
    
    # Filter sessions with content
    valid = []
    for sp in sessions:
        try:
            with open(sp) as f:
                sess = json.load(f)
            if sess.get("message_count", 0) >= 2:
                valid.append(sp)
        except:
            pass
    
    print(f"  Concurrency={concurrency}: {len(valid)} sessions loaded")
    
    # Submit all
    t0 = time.monotonic()
    sem = asyncio.Semaphore(concurrency)
    
    async def limited_submit(sp, idx):
        async with sem:
            return await submit_one(sp, wd, idx)
    
    tasks = [limited_submit(sp, i) for i, sp in enumerate(valid)]
    results = await asyncio.gather(*tasks)
    total_time = (time.monotonic() - t0) * 1000
    
    # Stats
    valid_results = [r for r in results if r]
    latencies = sorted([r["latency_ms"] for r in valid_results])
    passed_count = sum(1 for r in valid_results if r["passed"])
    
    if latencies:
        p50 = latencies[len(latencies)//2]
        p95 = latencies[int(len(latencies)*0.95)]
        p99 = latencies[int(len(latencies)*0.99)]
    else:
        p50 = p95 = p99 = 0
    
    # Gate-by-gate pass rates
    gate_stats = {}
    for r in valid_results:
        for gate, passed in r.get("gates", {}).items():
            if gate not in gate_stats:
                gate_stats[gate] = {"total": 0, "passed": 0}
            gate_stats[gate]["total"] += 1
            if passed:
                gate_stats[gate]["passed"] += 1
    
    return {
        "concurrency": concurrency,
        "total_submitted": len(valid),
        "total_results": len(valid_results),
        "total_time_ms": round(total_time, 1),
        "throughput_per_sec": round(len(valid_results) / (total_time/1000), 1),
        "passed_count": passed_count,
        "pass_rate": round(passed_count / max(len(valid_results), 1), 4),
        "latency_p50_ms": round(p50, 1),
        "latency_p95_ms": round(p95, 1),
        "latency_p99_ms": round(p99, 1),
        "latency_avg_ms": round(sum(latencies)/len(latencies), 1) if latencies else 0,
        "latency_min_ms": round(min(latencies), 1) if latencies else 0,
        "latency_max_ms": round(max(latencies), 1) if latencies else 0,
        "gate_pass_rates": {k: round(v["passed"]/max(v["total"],1), 4) for k, v in gate_stats.items()},
    }

async def main():
    print("=" * 70)
    print("agent-prod STRESS TEST — 156 real Hermes sessions")
    print("=" * 70)
    print()
    
    all_results = []
    for conc in CONCURRENCY_LEVELS:
        print(f"\n▶ Concurrency: {conc}")
        result = await stress_test(conc)
        all_results.append(result)
        
        print(f"   Submitted: {result['total_submitted']}")
        print(f"   Time: {result['total_time_ms']}ms")
        print(f"   Throughput: {result['throughput_per_sec']} sess/sec")
        print(f"   Pass rate: {result['pass_rate']*100:.1f}%")
        print(f"   Latency: P50={result['latency_p50_ms']}ms P95={result['latency_p95_ms']}ms P99={result['latency_p99_ms']}ms")
        print(f"   Gate pass rates: {result['gate_pass_rates']}")
    
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in all_results:
        print(f"  conc={r['concurrency']:4d}  {r['pass_rate']*100:5.1f}% pass  P50={r['latency_p50_ms']:6.1f}ms  P95={r['latency_p95_ms']:6.1f}ms  P99={r['latency_p99_ms']:6.1f}ms  {r['throughput_per_sec']:6.1f}/s")
    
    # Save results
    output_path = Path("data/stress_results/stress_live.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(all_results, indent=2))
    print(f"\nResults saved: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
