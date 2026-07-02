"""
quality_gates 端到端演示 — Phase 1 版本
与 Phase 0 行为兼容，所有 6 个场景应全部通过
"""
from datetime import datetime, timezone
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from agent_prod.gates.engine import QualityGateEngine, load_config
from agent_prod.gates.models import Improvement
from agent_prod.gates.repository import MemoryRepository


def _full_llm_call(response_id: str) -> dict:
    return {"response_id": response_id, "duration_ms": 500, "finish_reason": "stop"}


def _full_tool_call(request_id: str, tool: str = "search") -> dict:
    return {"request_id": request_id, "tool": tool, "duration_ms": 200, "success": True}


def scenario(name, baseline=None, candidate=None,
             llm_calls=None, tool_calls=None,
             trace_id="",
             actual_tokens=1_000,
             actual_time_ms=1_000,
             human_approver="",
             budget_tokens=100_000,
             budget_time_ms=60_000,
             expect_production=True):
    imp = Improvement(
        name=name,
        baseline_output=baseline or {},
        candidate_output=candidate or {},
        llm_calls=llm_calls or [],
        tool_calls=tool_calls or [],
        trace_id=trace_id,
        actual_tokens=actual_tokens,
        actual_time_ms=actual_time_ms,
        budget_tokens=budget_tokens,
        budget_time_ms=budget_time_ms,
    )
    if human_approver:
        imp.human_approver = human_approver
        imp.human_approved_at = datetime.now(timezone.utc)
    imp.metadata["expected"] = "production" if expect_production else "rejected"
    return imp


def build_scenarios():
    scenarios = []

    # 1. All gates pass
    scenarios.append(("All gates pass", scenario(
        name="perfect_iteration",
        baseline={"f1_score": 0.85, "latency_p95_ms": 100, "success_rate": 0.99},
        candidate={
            "final_response": "improved output",
            "confidence": 0.95,
            "tools_used": ["search"],
            "token_count": 1_000,
            "warnings": [],
            "f1_score": 0.87,
            "latency_p95_ms": 95,
            "success_rate": 0.99,
        },
        llm_calls=[_full_llm_call("r1")],
        tool_calls=[_full_tool_call("r1")],
        human_approver="alice@example.com",
        expect_production=True,
    )))

    # 2. Gate1: Schema violation
    scenarios.append(("Gate1: Schema violation", scenario(
        name="schema_violation",
        candidate={"bad_field": "no schema match"},
        expect_production=False,
    )))

    # 3. Gate2: Orphan tool calls
    scenarios.append(("Gate2: Orphan tool calls", scenario(
        name="orphan_tools",
        candidate={
            "final_response": "valid output",
            "confidence": 0.9,
            "tools_used": ["search"],
            "token_count": 500,
            "warnings": [],
        },
        actual_tokens=500,
        llm_calls=[_full_llm_call("r1")],
        tool_calls=[_full_tool_call("orphan_1")],
        expect_production=False,
    )))

    # 4. Gate3: Regression detected
    scenarios.append(("Gate3: Regression detected", scenario(
        name="regression",
        baseline={"f1_score": 0.85, "latency_p95_ms": 100, "success_rate": 0.99},
        candidate={
            "final_response": "valid output",
            "confidence": 0.95,
            "tools_used": ["search"],
            "token_count": 1_000,
            "warnings": [],
            "f1_score": 0.50,
            "latency_p95_ms": 95,
            "success_rate": 0.99,
        },
        expect_production=False,
    )))

    # 5. Gate5: Missing human approval
    scenarios.append(("Gate5: Missing human approval (1)", scenario(
        name="gray_failure",
        baseline={"latency_p95_ms": 100},
        candidate={
            "final_response": "valid output",
            "confidence": 0.95,
            "tools_used": ["search"],
            "token_count": 1_000,
            "warnings": [],
        },
        llm_calls=[_full_llm_call("r1")],
        tool_calls=[_full_tool_call("r1")],
        expect_production=False,
    )))

    # 6. Gate5: Missing human approval (2)
    scenarios.append(("Gate5: Missing human approval (2)", scenario(
        name="no_approval",
        baseline={"f1_score": 0.85, "latency_p95_ms": 100, "success_rate": 0.99},
        candidate={
            "final_response": "improved output",
            "confidence": 0.95,
            "tools_used": ["search"],
            "token_count": 1_000,
            "warnings": [],
            "f1_score": 0.87,
            "latency_p95_ms": 95,
            "success_rate": 0.99,
        },
        llm_calls=[_full_llm_call("r1")],
        tool_calls=[_full_tool_call("r1")],
        human_approver="",
        expect_production=False,
    )))

    return scenarios


def run_demo():
    scenarios = build_scenarios()
    config = load_config()
    if "gates" not in config:
        config["gates"] = {}
    if "gate4" not in config["gates"]:
        config["gates"]["gate4"] = {}
    config["gates"]["gate4"]["metrics_provider"] = "demo"

    repo = MemoryRepository()
    engine = QualityGateEngine(
        repository=repo,
        config=config,
        gate_timeout_seconds=30.0,
    )

    print("=" * 70)
    print("  Quality Gates \u2014 Phase 1 Demo")
    print(f"  {len(scenarios)} scenarios to run")
    print("=" * 70)
    print()

    passed = 0
    failed = 0

    for idx, (label, improvement) in enumerate(scenarios, 1):
        print(f"[{idx}/{len(scenarios)}] {label}")
        result = engine.run_pipeline(improvement, persist=True)
        expected = improvement.metadata.get("expected", "production")
        actual = result.status.value
        ok = actual == expected
        check = "\u2705" if ok else "\u274c"
        print(f"      Result: {result.status.value} {check} (expected: {expected})")
        for gr in result.gate_results:
            st = "\u2705" if gr.passed else "\u274c"
            print(f"      {st} {gr.gate_name}: {gr.reason}")
        print()
        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 70)
    print(f"  Total: {passed} passed, {failed} failed ({len(scenarios)} scenarios)")
    print("=" * 70)

    saved = repo.list(limit=100)
    print(f"\n  Repository: {len(saved)} improvements persisted")
    for imp in saved:
        gates_str = ", ".join(
            f"{gr.gate_name}:{'\u2705' if gr.passed else '\u274c'}"
            for gr in imp.gate_results
        )
        print(f"    {imp.name}: {imp.status.value} [{gates_str}]")

    return failed == 0


if __name__ == "__main__":
    success = run_demo()
    sys.exit(0 if success else 1)
# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)
