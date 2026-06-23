"""
quality_gates 端到端演示 — Phase 1 版本
与 Phase 0 行为兼容，所有 6 个场景应全部通过
"""
import sys
import os

# 将父目录加入 sys.path 使 from agent_prod.gates.* 导入正确
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from agent_prod.gates.models import Improvement
from agent_prod.gates.engine import QualityGateEngine, load_config
from agent_prod.gates.repository import MemoryRepository


# ── 场景定义 ──────────────────────────────────────────────────

def scenario(name: str, baseline: dict | None = None,
             candidate: dict | None = None,
             llm_calls: list | None = None,
             tool_calls: list | None = None,
             trace_id: str = "",
             actual_tokens: int = 1_000,
             actual_time_ms: int = 1_000,
             human_approver: str = "",
             budget_tokens: int = 100_000,
             budget_time_ms: int = 60_000,
             expect_production: bool = True) -> Improvement:
    """创建一个演示场景的 improvement"""
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
        imp.human_approved_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    imp.metadata["expected"] = "production" if expect_production else "rejected"
    return imp


def build_scenarios() -> list[tuple[str, Improvement]]:
    """构建 6 个演示场景"""
    scenarios = []

    # 1. 完全通过 — 所有门都符合预期
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
        llm_calls=[{"response_id": "r1"}],
        tool_calls=[{"request_id": "r1"}],
        human_approver="alice@example.com",
        expect_production=True,
    )))

    # 2. Gate1 失败 — 输出不符合 schema
    scenarios.append(("Gate1: Schema violation", scenario(
        name="schema_violation",
        candidate={"bad_field": "no schema match"},
        expect_production=False,
    )))

    # 3. Gate2 失败 — 孤儿工具调用
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
        llm_calls=[{"response_id": "r1"}],
        tool_calls=[{"request_id": "orphan_1"}],  # 没有对应的 LLM call
        expect_production=False,
    )))

    # 4. Gate3 失败 — 回归检测到关键指标下降
    scenarios.append(("Gate3: Regression detected", scenario(
        name="regression",
        baseline={"f1_score": 0.85, "latency_p95_ms": 100, "success_rate": 0.99},
        candidate={
            "final_response": "valid output",
            "confidence": 0.95,
            "tools_used": ["search"],
            "token_count": 1_000,
            "warnings": [],
            "f1_score": 0.50,  # 明显降级
            "latency_p95_ms": 95,
            "success_rate": 0.99,
        },
        expect_production=False,
    )))

    # 5. Gate5 失败 — 缺少人工审批（这个场景 Gate4 通过但 Gate5 拒绝）
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
        llm_calls=[{"response_id": "r1"}],
        tool_calls=[{"request_id": "r1"}],
        expect_production=False,
    )))

    # 6. 全部通过（无审批人 — Gate5 人工审批缺失）
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
        llm_calls=[{"response_id": "r1"}],
        tool_calls=[{"request_id": "r1"}],
        human_approver="",  # 没有审批人
        expect_production=False,
    )))

    return scenarios


# ── 主函数 ──────────────────────────────────────────────────

def run_demo():
    """运行所有场景"""
    scenarios = build_scenarios()

    # 加载配置（使用 demo 模式指标）
    config = load_config()
    # 强制使用 demo 指标提供者
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
    print("  Quality Gates — Phase 1 Demo")
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

        print(f"      Result: {result.status.value} "
              f"{'✅' if ok else '❌'} "
              f"(expected: {expected})")
        for gr in result.gate_results:
            status = "✅" if gr.passed else "❌"
            print(f"      {status} {gr.gate_name}: {gr.reason}")
        print()

        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 70)
    print(f"  Total: {passed} passed, {failed} failed "
          f"({len(scenarios)} scenarios)")
    print("=" * 70)

    # 验证持久化
    saved = repo.list(limit=100)
    print(f"\n  Repository: {len(saved)} improvements persisted")
    for imp in saved:
        gates_str = ", ".join(
            f"{gr.gate_name}:{'✅' if gr.passed else '❌'}"
            for gr in imp.gate_results
        )
        print(f"    {imp.name}: {imp.status.value} [{gates_str}]")

    return failed == 0


if __name__ == "__main__":
    success = run_demo()
    sys.exit(0 if success else 1)
