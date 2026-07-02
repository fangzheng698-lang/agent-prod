# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

#!/usr/bin/env python3
"""Benchmark 自动跑分 — 对 Gate6 答案质量做定量评估。

用法:
    python -m agent_prod.benchmark.run                    # 全部
    python -m agent_prod.benchmark.run --cat config       # 按分类
    python -m agent_prod.benchmark.run --difficulty hard  # 按难度
    python -m agent_prod.benchmark.run --id bm-001        # 单题

输出:
    ┌─────────────────────────────────────────┐
    │  Benchmark Results: 12/15 PASS (80.0%)  │
    │  Avg Score: 0.82  Min: 0.45  Max: 0.95  │
    │  By category: config 4/4, arch 5/7, ... │
    └─────────────────────────────────────────┘
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import yaml

BENCHMARK_FILE = Path(__file__).parent / "benchmarks.yaml"
DEFAULT_URL = "http://localhost:8765"


def load_benchmarks(path: Path | None = None) -> list[dict]:
    path = path or BENCHMARK_FILE
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("benchmarks", [])


def run_single(bm: dict, base_url: str, timeout: int = 60) -> dict:
    """对单道 benchmark 题发 evaluate 请求，返回 Gate6 结果。"""
    # candidate_answer: 有则用，空字符串也是有效变体（表示空回答）
    raw_candidate = bm.get("candidate_answer")
    if raw_candidate is not None:
        candidate = raw_candidate
    else:
        candidate = bm.get("expected_answer", "")
    payload = {
        "agent": "benchmark-agent",
        "session_id": f"bm-{bm['id']}",
        "output": {
            "final_response": candidate,
            "expected_answer": bm.get("expected_answer", candidate),
        },
        "decisions": [
            {
                "decision_id": "turn-1",
                "model": "benchmark",
                "prompt_tokens": 100,
                "completion_tokens": 80,
                "tool_calls": [],
            }
        ],
        "current_metrics": {
            "latency_p95_ms": 1000,
            "success_rate": 1.0,
            "error_rate": 0.0,
            "custom": {"user_question": bm["question"]},
        },
    }

    url = f"{base_url.rstrip('/')}/v1/agent/evaluate"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return {
            "id": bm["id"],
            "category": bm.get("category", "?"),
            "difficulty": bm.get("difficulty", "?"),
            "error": str(e),
        }

    gates = result.get("gates", [])
    g6 = next((g for g in gates if "gate6" in g.get("gate", "").lower()), {})

    return {
        "id": bm["id"],
        "question": bm["question"][:80],
        "category": bm.get("category", "?"),
        "difficulty": bm.get("difficulty", "?"),
        "passed": g6.get("passed", False),
        "score": g6.get("details", {}).get("score", 0),
        "method": g6.get("details", {}).get("method", "?"),
        "duration_ms": g6.get("duration_ms", 0),
        "status": result.get("status", "?"),
    }


def run_benchmarks(
    benchmarks: list[dict],
    base_url: str,
    timeout: int = 60,
    cat_filter: str = "",
    diff_filter: str = "",
    id_filter: str = "",
) -> list[dict]:
    results = []
    total = len(benchmarks)
    for i, bm in enumerate(benchmarks):
        if cat_filter and bm.get("category") != cat_filter:
            continue
        if diff_filter and bm.get("difficulty") != diff_filter:
            continue
        if id_filter and bm["id"] != id_filter:
            continue

        # 逐条跑，避免服务器过载
        if i > 0:
            time.sleep(0.3)

        r = run_single(bm, base_url, timeout)
        passed = "PASS" if r.get("passed") else "FAIL"
        score = r.get("score", "?")
        err = r.get("error", "")
        marker = "❌" if err else ("✅" if r.get("passed") else "⚠️")
        print(f"  {marker} {r['id']} [{r.get('category','?')}] score={score:.2f} {passed}")
        if err:
            print(f"       Error: {err[:100]}")
        results.append(r)

    return results


def print_summary(results: list[dict]):
    if not results:
        print("\nNo results.")
        return

    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    pct = (passed / total * 100) if total else 0

    scores = [r.get("score", 0) for r in results if r.get("score") is not None]
    avg_score = sum(scores) / len(scores) if scores else 0
    min_score = min(scores) if scores else 0
    max_score = max(scores) if scores else 0

    errors = [r for r in results if r.get("error")]

    # 按分类统计
    by_cat: dict[str, tuple[int, int]] = {}
    for r in results:
        cat = r.get("category", "?")
        p, t = by_cat.get(cat, (0, 0))
        by_cat[cat] = (p + (1 if r.get("passed") else 0), t + 1)

    print(f"\n{'='*55}")
    print(f"  Benchmark Results: {passed}/{total} PASS ({pct:.1f}%)")
    print(f"  Avg Score: {avg_score:.2f}  Min: {min_score:.2f}  Max: {max_score:.2f}")
    if errors:
        print(f"  Errors: {len(errors)}")
    print(f"  By category:")
    for cat in sorted(by_cat):
        p, t = by_cat[cat]
        print(f"    {cat}: {p}/{t} ({p/t*100:.0f}%)" if t else f"    {cat}: 0/0")
    print(f"{'='*55}")


def main():
    parser = argparse.ArgumentParser(description="agent-prod Benchmark Runner")
    parser.add_argument("--url", default=DEFAULT_URL, help="agent-prod server URL")
    parser.add_argument("--cat", default="", help="Filter by category")
    parser.add_argument("--difficulty", default="", help="Filter by difficulty")
    parser.add_argument("--id", default="", help="Run single benchmark by ID")
    parser.add_argument("--timeout", type=int, default=90, help="Request timeout (s)")
    args = parser.parse_args()

    benchmarks = load_benchmarks()
    if not benchmarks:
        print("No benchmarks found.")
        sys.exit(1)

    print(f"Running {len(benchmarks)} benchmarks against {args.url}...\n")
    results = run_benchmarks(
        benchmarks, args.url, args.timeout,
        args.cat, args.difficulty, args.id,
    )
    print_summary(results)

    # Exit code: non-zero if any failed
    failed = sum(1 for r in results if not r.get("passed") and not r.get("error"))
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
