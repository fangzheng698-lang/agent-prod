"""多审批节点·多智能体·长周期运作 压力测试。

场景:
  3 个 Sprint、9 轮提交、4 种 Agent、3 个审批人。
  模拟 AI 研发团队从架构→编码→测试→发布的完整生命周期。

  Sprint 1 — 架构设计阶段
    轮 1: architect-agent → tech-lead 审批 → 高质量 → PRODUCTION
    轮 2: architect-agent → tech-lead 审批 → 高质量 → PRODUCTION

  Sprint 2 — 编码实现阶段
    轮 3: coder-agent    → senior-dev 审批 → 高质量 → PRODUCTION
    轮 4: coder-agent    → senior-dev 审批 → 低质量 → ❌ Gate3 回归拒绝
    轮 5: coder-agent    → senior-dev 审批 → 高质量 → PRODUCTION (恢复)

  Sprint 3 — 测试与发布阶段
    轮 6: tester-agent   → qa-lead 审批   → 高质量 → PRODUCTION
    轮 7: reviewer-agent → senior-dev 审批 → 低质量 → ❌ Gate6 质量不达标
    轮 8: reviewer-agent → senior-dev 审批 → 高质量 → PRODUCTION (恢复)
    轮 9: architect-agent → tech-lead 审批 → 高质量 → PRODUCTION (最终验收)

验证目标:
  1. 多审批节点: tech-lead / senior-dev / qa-lead 各自独立审批通过 Gate5
  2. 多智能体独立基线: 4 个 agent 各自维护 Gate3 基线
  3. 长周期回归检测: 轮 4 (coder-agent, 低质量) 被 Gate3 拒绝
  4. Gate6 质量评估: 轮 7 (reviewer-agent, 低质量) 被 Gate6 拒绝
  5. 恢复能力: 轮 5 和轮 8 恢复高质量后通过
  6. 最终验收: 轮 9 双 sprint 成果合并后通过
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from agent_prod import trace

# ── 路径 ──
CONFIG_PATH = Path(__file__).resolve().parent.parent / "src" / "agent_prod" / "gates" / "config.yaml"
DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ═══════════════════════════════════════════════════════════════════
#  9 轮数据定义 — 3 Sprint × 3 轮 = 9 轮
# ═══════════════════════════════════════════════════════════════════

HIGH_FINAL = (
    "## 架构设计评审报告\n\n"
    "### 整体评估\n"
    "本次评审了订单服务的架构设计方案，总体质量良好。\n\n"
    "### 1. 服务拆分\n"
    "- 订单服务: 独立部署，负责订单生命周期管理 ✓\n"
    "- 支付服务: 异步事件驱动 ✓\n"
    "- 库存服务: 预留缓存层设计 ✓\n\n"
    "### 2. 数据一致性\n"
    "- Saga 模式处理分布式事务 ✓\n"
    "- 幂等性设计覆盖所有关键接口 ✓\n\n"
    "### 3. 性能评估\n"
    "- 预估 TPS: 5000/s，峰值 12000/s\n"
    "- 缓存: Redis 集群，命中率目标 >95%\n\n"
    "### 结论\n"
    "架构设计符合规范，评审通过。"
)

CODER_HIGH = (
    "## 核心代码实现\n\n"
    "### 订单服务实现\n"
    "- 创建订单接口: POST /api/v1/orders ✓\n"
    "- 订单状态机: PENDING → PAID → SHIPPED → DELIVERED ✓\n"
    "- 幂等性: 订单号 + 请求 ID 去重 ✓\n\n"
    "### 支付集成\n"
    "- 支付网关适配器模式: 支持微信/支付宝 ✓\n"
    "- 回调处理: 签名验证 + 幂等处理 ✓\n"
    "- 退款流程: 异步处理 + 通知 ✓\n\n"
    "### 测试覆盖\n"
    "- 单元测试覆盖核心逻辑 >90%\n"
    "- 集成测试覆盖全部 API 端点\n"
    "- 压力测试: 1000 并发稳定运行"
)

CODER_LOW = (
    "代码写完了，跑了一下好像没啥问题。接口都通了，"
    "测试也过了，可以上线。"
)

TESTER_HIGH = (
    "## 测试报告\n\n"
    "### 功能测试\n"
    "- 订单创建: 50 个用例全部通过 ✓\n"
    "- 支付回调: 20 个用例全部通过 ✓\n"
    "- 退款流程: 15 个用例全部通过 ✓\n\n"
    "### 性能测试\n"
    "- 500 并发: P95 响应时间 320ms\n"
    "- 1000 并发: P95 响应时间 580ms\n"
    "- CPU 使用率峰值 <70%\n\n"
    "### 安全测试\n"
    "- SQL 注入: 全部拦截 ✓\n"
    "- XSS: 全部转义 ✓\n"
    "- CSRF: Token 验证全部通过 ✓"
)

REVIEWER_HIGH = (
    "## 代码审查报告\n\n"
    "### 整体评价\n"
    "代码质量良好，架构清晰。\n\n"
    "### 1. 代码规范\n"
    "- 命名规范: 符合团队规范 ✓\n"
    "- 代码注释: 核心逻辑有适当注释 ✓\n"
    "- 错误处理: 覆盖所有异常路径 ✓\n\n"
    "### 2. 架构评审\n"
    "- 分层清晰: Controller → Service → Repository ✓\n"
    "- 依赖注入: 使用接口抽象 ✓\n"
    "- 配置外部化: 无硬编码配置 ✓\n\n"
    "### 3. 潜在问题\n"
    "- 建议补充更多边界测试用例\n"
    "- 部分方法偏长，建议拆分\n\n"
    "### 结论\n"
    "代码审查通过，建议优化后合入主分支。"
)

REVIEWER_LOW = (
    "看了下代码，写得不错，没啥大问题，同意合并。"
)



FINAL_ARCH_HIGH = (
    "## 最终架构验收评审报告\n\n"
    "### 整体评估\n"
    "双 Sprint 成果合并验收，全部达标。\n\n"
    "### 1. 架构设计\n"
    "- 服务拆分: 订单/支付/库存独立部署 ✓\n"
    "- 数据一致性: Saga 模式 ✓\n"
    "- 容灾方案: 多活部署 ✓\n\n"
    "### 2. 编码实现\n"
    "- 订单服务 API 全部实现并测试 ✓\n"
    "- 支付集成通过 ✓\n"
    "- 测试覆盖率 >90% ✓\n\n"
    "### 3. 安全审查\n"
    "- SQL 注入/XSS/CSRF 全部防护 ✓\n"
    "- 安全审计通过 ✓\n\n"
    "### 结论\n"
    "架构设计、编码实现、测试覆盖全部达标，验收通过。"
)


ROUNDS = [
    # ── Sprint 1: 架构设计 ──
    {
        "sprint": 1,
        "num": 1,
        "agent": "architect-agent",
        "session_id": "sprint1_arch_v1",
        "human_approver": "tech-lead",
        "quality": "high",
        "success_rate": 0.98,
        "error_rate": 0.02,
        "token_efficiency": 0.90,
        "latency_p95_ms": 1200,
        "final_response": FINAL_ARCH_HIGH,
        "token_count": 4500,
        "tool_calls": 6,
        "expected_plan": "对订单服务做架构设计评审：服务拆分、数据一致性、性能评估、安全审查",
        "expect": "PRODUCTION",
        "desc": "架构设计 v1 — tech-lead 审批",
    },
    {
        "sprint": 1,
        "num": 2,
        "agent": "architect-agent",
        "session_id": "sprint1_arch_v2",
        "human_approver": "tech-lead",
        "quality": "high",
        "success_rate": 0.98,
        "error_rate": 0.02,
        "token_efficiency": 0.92,
        "latency_p95_ms": 1200,     # 与轮 1 一致，避免 Gate3 误判
        "final_response": FINAL_ARCH_HIGH,
        "token_count": 4200,
        "tool_calls": 5,
        "expected_plan": "对订单服务做架构设计评审：服务拆分、数据一致性、性能评估、安全审查",
        "expect": "PRODUCTION",
        "desc": "架构设计 v2 (迭代) — tech-lead 审批",
    },
    # ── Sprint 2: 编码实现 ──
    {
        "sprint": 2,
        "num": 3,
        "agent": "coder-agent",
        "session_id": "sprint2_code_v1",
        "human_approver": "senior-dev",
        "quality": "high",
        "success_rate": 0.98,
        "error_rate": 0.02,
        "token_efficiency": 0.88,
        "latency_p95_ms": 1500,
        "final_response": CODER_HIGH,
        "token_count": 8000,
        "tool_calls": 10,
        "expected_plan": "实现订单服务核心代码：创建订单接口、订单状态机、支付集成、测试覆盖 >90%",
        "expect": "PRODUCTION",
        "desc": "核心代码实现 v1 — senior-dev 审批",
    },
    {
        "sprint": 2,
        "num": 4,
        "agent": "coder-agent",
        "session_id": "sprint2_code_v2_bad",
        "human_approver": "senior-dev",
        "quality": "low",
        "success_rate": 0.50,
        "error_rate": 0.50,
        "token_efficiency": 0.30,
        "latency_p95_ms": 6000,
        "final_response": CODER_LOW,
        "token_count": 500,
        "tool_calls": 2,
        "expected_plan": "实现订单服务核心代码：创建订单接口、订单状态机、支付集成、测试覆盖 >90%",
        "expect": "GATE3_REG",
        "desc": "代码质量下降 → 预期 Gate3 回归拒绝",
    },
    {
        "sprint": 2,
        "num": 5,
        "agent": "coder-agent",
        "session_id": "sprint2_code_v3_fixed",
        "human_approver": "senior-dev",
        "quality": "high",
        "success_rate": 0.98,
        "error_rate": 0.02,
        "token_efficiency": 0.90,
        "latency_p95_ms": 1500,     # 与轮 3 一致，避免 Gate3 误判
        "final_response": CODER_HIGH,
        "token_count": 7500,
        "tool_calls": 9,
        "expected_plan": "修复代码质量问题后重新实现订单服务核心代码",
        "expect": "PRODUCTION",
        "desc": "修复后重新提交 — senior-dev 审批",
    },
    # ── Sprint 3: 测试与发布 ──
    {
        "sprint": 3,
        "num": 6,
        "agent": "tester-agent",
        "session_id": "sprint3_test_v1",
        "human_approver": "qa-lead",
        "quality": "high",
        "success_rate": 0.99,
        "error_rate": 0.01,
        "token_efficiency": 0.85,
        "latency_p95_ms": 1800,
        "final_response": TESTER_HIGH,
        "token_count": 6000,
        "tool_calls": 8,
        "expected_plan": "执行全量测试：功能测试、500/1000 并发性能测试、SQL注入/XSS/CSRF 安全测试",
        "expect": "PRODUCTION",
        "desc": "测试报告 — qa-lead 审批",
    },
    {
        "sprint": 3,
        "num": 7,
        "agent": "reviewer-agent",
        "session_id": "sprint3_review_v1_bad",
        "human_approver": "senior-dev",
        "quality": "low",
        "success_rate": 1.0,
        "error_rate": 0.0,
        "token_efficiency": 0.95,
        "latency_p95_ms": 800,
        "final_response": REVIEWER_LOW,
        "token_count": 300,
        "tool_calls": 3,
        "expected_plan": "代码审查：检查代码规范、架构合理性、安全漏洞、给出详细改进建议",
        "expect": "GATE6_FAIL",
        "desc": "敷衍审查 → 预期 Gate6 拒绝",
    },
    {
        "sprint": 3,
        "num": 8,
        "agent": "reviewer-agent",
        "session_id": "sprint3_review_v2_good",
        "human_approver": "senior-dev",
        "quality": "high",
        "success_rate": 1.0,
        "error_rate": 0.0,
        "token_efficiency": 0.93,
        "latency_p95_ms": 900,
        "final_response": REVIEWER_HIGH,
        "token_count": 5500,
        "tool_calls": 7,
        "expected_plan": "代码审查：检查代码规范、架构合理性、安全漏洞、给出详细改进建议",
        "expect": "PRODUCTION",
        "desc": "认真审查后重新提交 — senior-dev 审批",
    },
    {
        "sprint": 3,
        "num": 9,
        "agent": "architect-agent",
        "session_id": "sprint3_final_arch",
        "human_approver": "tech-lead",
        "quality": "high",
        "success_rate": 0.98,        # 与轮 1/2 一致
        "error_rate": 0.02,
        "token_efficiency": 0.94,
        "latency_p95_ms": 1200,      # 与轮 1/2 一致
        "final_response": FINAL_ARCH_HIGH,
        "token_count": 5000,
        "tool_calls": 6,
        "expected_plan": "最终架构验收评审：确认架构设计、编码实现、测试覆盖全部达标",
        "expect": "PRODUCTION",
        "desc": "最终架构验收 — tech-lead 审批 (双 Sprint 成果合并)",
    },
]


# ═══════════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════════

def quality_label(q: str) -> str:
    return {"high": f"{GREEN}高质量{RESET}", "low": f"{RED}低质量{RESET}"}.get(q, q)


def expect_label(e: str) -> str:
    labels = {
        "PRODUCTION": f"{GREEN}✅ PRODUCTION{RESET}",
        "GATE3_REG": f"{RED}❌ Gate3 回归拒绝{RESET}",
        "GATE6_FAIL": f"{RED}❌ Gate6 质量不达标{RESET}",
    }
    return labels.get(e, e)


def round_match(rd: dict, result: dict) -> tuple[bool, str]:
    """检查一轮结果是否匹配预期。"""
    passed = result.get("passed", False)
    status = result.get("status", "?")
    failed_at = result.get("failed_at", "")

    exp = rd["expect"]

    if exp == "PRODUCTION":
        ok = passed and status.lower() == "production"
        return ok, "通过" if ok else f"状态={status}, passed={passed}"
    elif exp == "GATE3_REG":
        ok = not passed and "gate3" in failed_at.lower()
        return ok, f"failed_at={failed_at}" if not ok else "Gate3 正确拒绝"
    elif exp == "GATE6_FAIL":
        ok = not passed and "gate6" in failed_at.lower()
        return ok, f"failed_at={failed_at}" if not ok else "Gate6 正确拒绝"
    else:
        return False, f"未知预期: {exp}"


# ═══════════════════════════════════════════════════════════════════
#  环境配置
# ═══════════════════════════════════════════════════════════════════

def ensure_config():
    """配置 FileRepository + Gate3 基线 + Gate6 API key."""
    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if not backup_path.exists():
        backup_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
        print(f"  Config backup: {backup_path}")

    # storage
    config.setdefault("storage", {})["backend"] = "file"
    config["storage"]["file_path"] = str(
        Path(__file__).resolve().parent.parent / "data" / "long_cycle_test.json"
    )

    # 添加测试 agent 到 Gate0 observe
    gate0 = config.setdefault("gates", {}).setdefault("gate0", {})
    per_agent = gate0.setdefault("per_agent", {})
    for agent_name in ["architect-agent", "coder-agent", "tester-agent", "reviewer-agent"]:
        if agent_name not in per_agent:
            per_agent[agent_name] = {"mode": "observe"}
        else:
            per_agent[agent_name]["mode"] = "observe"

    # Gate3 动态基线
    gate3 = config.setdefault("gates", {}).setdefault("gate3", {})
    gate3["dynamic_baseline"] = True
    gate3["auto_evolve_baseline"] = True
    gate3["baseline_min_samples"] = 1

    # Gate6 API key
    dotenv_key = None
    if DOTENV_PATH.exists():
        for line in DOTENV_PATH.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                dotenv_key = line.split("=", 1)[1].strip().strip("\"'")
                break

    gate6 = config.setdefault("gates", {}).setdefault("gate6", {})
    if dotenv_key:
        gate6["llm_api_key"] = dotenv_key
    gate6["pass_threshold"] = 0.58
    gate6["enabled"] = True
    gate6["evaluator"] = "checklist"

    CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    print(f"  Config written: storage=file, Gate3 dynamic_baseline=True, Gate6 api_key={'present' if dotenv_key else 'MISSING'}")


def restore_config():
    """恢复备份的配置."""
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if backup_path.exists():
        CONFIG_PATH.write_text(backup_path.read_text())
        backup_path.unlink()
        print(f"  Config restored from backup")


def ensure_server():
    """确保 server 以 production mode 运行。"""
    # 关闭已有 server
    try:
        pid_str = subprocess.run(
            ["lsof", "-ti", ":8000"], capture_output=True, text=True
        ).stdout.strip()
        if pid_str:
            for pid in pid_str.split():
                os.kill(int(pid), signal.SIGTERM)
            time.sleep(2)
            print(f"  Stopped existing server(s)")
    except Exception:
        pass

    # 读取 .env 中的 API key
    env = os.environ.copy()
    env["QUALITY_GATES_MODE"] = "production"
    if DOTENV_PATH.exists():
        for line in DOTENV_PATH.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")

    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_prod", "serve", "--port", "8000"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  Started server (PID {proc.pid}) with QUALITY_GATES_MODE=production")
    time.sleep(4)

    # 验证健康状态
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://localhost:8000/health", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"  {GREEN}Server health: {data.get('status')}{RESET}")
    except Exception as e:
        print(f"  {RED}Server failed to start: {e}{RESET}")
        sys.exit(1)

    return proc


# ═══════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════

def build_decisions(rd: dict):
    """根据 round 数据生成 decisions 列表。"""
    tc_count = rd["tool_calls"]
    tool_calls = []
    for i in range(tc_count):
        tool_calls.append({
            "tool_id": f"r{rd['num']}-tc-{i+1}",
            "tool_name": "read_file" if i % 2 == 0 else "search_files",
            "arguments": {"path": f"src/sprint{rd['sprint']}/"},
            "result_summary": f"工具调用 {i+1}/{tc_count}",
            "success": True,
            "duration_ms": 200.0 + (i * 50),
        })

    return [{
        "decision_id": f"r{rd['num']}-d1",
        "model": "gpt-4",
        "prompt_tokens": 2000,
        "completion_tokens": rd["token_count"] - 2000 if rd["token_count"] > 2000 else 500,
        "reasoning": f"Sprint {rd['sprint']}, 轮 {rd['num']}: {rd['desc']}",
        "tool_calls": tool_calls,
    }]


def print_round_header(rd: dict):
    sprint = rd["sprint"]
    num = rd["num"]
    phase = {1: "架构", 2: "编码", 3: "测试/发布"}[sprint]
    print(f"\n{BOLD}── Sprint {sprint} ({phase}) · 轮 {num}/9 ──{RESET}")
    print(f"  Agent: {CYAN}{rd['agent']}{RESET} | 审批: {YELLOW}{rd['human_approver']}{RESET} | "
          f"质量: {quality_label(rd['quality'])}")
    print(f"  预期: {expect_label(rd['expect'])}")
    print(f"  {rd['desc']}")


def print_round_result(num: int, rd: dict, result: dict, match: bool, detail: str):
    passed = result.get("passed", False)
    status = result.get("status", "?")
    failed_at = result.get("failed_at", "")
    gates_passed = len([g for g in result.get("gates", []) if g.get("passed", False)])
    total_gates = len(result.get("gates", []))

    icon = "✅" if passed else "❌"
    match_icon = "✓" if match else "✗"
    match_color = GREEN if match else RED

    print(f"  {icon} {BOLD}结果: {status}{RESET} "
          f"({gates_passed}/{total_gates} 门通过) "
          f"匹配: {match_color}{match_icon}{RESET}")
    if failed_at:
        reason = result.get("fail_reason", "")
        print(f"     {RED}⛔ 拒绝于: {failed_at}{RESET}")
        if reason:
            print(f"     原因: {reason[:200]}")

    # Gate7 详情（观察者模式，只记录不阻断）
    for g in result.get("gates", []):
        gn = g.get("gate_name", g.get("gate", ""))
        if "gate7" in str(gn).lower():
            det = g.get("details", {})
            if det.get("skipped"):
                print(f"     ℹ️  Gate7: 跳过（无 expected_plan）")
            else:
                devs = det.get("deviations", [])
                mode = det.get("mode", "observe")
                if devs:
                    critical = [d for d in devs if d.get("severity") == "critical"]
                    warnings = [d for d in devs if d.get("severity") == "warning"]
                    parts = []
                    if critical:
                        parts.append(f"{RED}{len(critical)} critical{RESET}")
                    if warnings:
                        parts.append(f"{YELLOW}{len(warnings)} warning{RESET}")
                    dev_summary = ", ".join(parts) if parts else f"{len(devs)} info"
                    print(f"     🔍 Gate7 [{mode}] {dev_summary}:")
                    for d in devs[:2]:
                        print(f"        {d['type']}: {d.get('detail', '')[:100]}")
                else:
                    print(f"     ✅ Gate7 [{mode}]: 按计划执行")
            break


def main():
    print()
    print("=" * 68)
    print(f"  {BOLD}多审批节点 · 多智能体 · 长周期运作 压力测试{RESET}")
    print(f"  Multi-Approval Long-Cycle Stress Test")
    print("=" * 68)
    print()
    print(f"  场景: 3 Sprint × 9 轮 = AI 研发团队完整交付周期")
    print(f"  Agent: architect-agent → coder-agent → tester-agent → reviewer-agent")
    print(f"  审批: tech-lead → senior-dev → qa-lead")
    print()

    # ── 1. 配置 ──
    print(f"{BOLD}[准备] 配置环境{RESET}")
    print("-" * 40)
    ensure_config()
    print()

    # ── 2. 启动 server ──
    print(f"{BOLD}[准备] 启动 server{RESET}")
    print("-" * 40)
    server_proc = ensure_server()
    print()

    all_results = []
    all_passed = True

    try:
        # ── 3. 执行 9 轮 ──
        for rd in ROUNDS:
            print_round_header(rd)

            decisions = build_decisions(rd)
            result = trace(
                agent=rd["agent"],
                session_id=rd["session_id"],
                version="1.0.0",
                decisions=decisions,
                current_metrics={
                    "latency_p95_ms": rd["latency_p95_ms"],
                    "success_rate": rd["success_rate"],
                    "error_rate": rd["error_rate"],
                    "token_efficiency": rd["token_efficiency"],
                    "final_response": rd["final_response"],
                    "expected_plan": rd.get("expected_plan", ""),
                    "user_question": f"请完成以下任务: {rd.get('expected_plan', '')}",
                },
                traffic_percentage=100,
                human_approver=rd["human_approver"],
                declared_tools=["read_file", "search_files"],
                budget_tokens=50000,
                budget_time_ms=300000,
            )

            match, detail = round_match(rd, result)
            print_round_result(rd["num"], rd, result, match, detail)
            all_results.append((rd, result, match, detail))
            if not match:
                all_passed = False

            # 轮间等待，确保仓库写入 + 时间戳区分
            time.sleep(1.5)

        # ═══════════════════════════════════════════════════════════════
        #  汇总报告
        # ═══════════════════════════════════════════════════════════════

        print()
        print("=" * 68)
        print(f"  {BOLD}测试完成 — 结果汇总{RESET}")
        print("=" * 68)
        print()

        # ── Sprint 汇总 ──
        for sprint_num in [1, 2, 3]:
            sprint_rounds = [(rd, r, m, d) for rd, r, m, d in all_results if rd["sprint"] == sprint_num]
            phase = {1: "架构设计", 2: "编码实现", 3: "测试与发布"}[sprint_num]
            print(f"  {BOLD}Sprint {sprint_num}: {phase}{RESET}")
            for rd, result, match, detail in sprint_rounds:
                passed = result.get("passed", False)
                icon = "✅" if passed else "❌"
                status = result.get("status", "?")
                failed_at = result.get("failed_at", "")
                match_str = f"{GREEN}✓{RESET}" if match else f"{RED}✗{RESET}"
                print(f"    {icon} 轮 {rd['num']:1d} | {rd['agent']:18s} | "
                      f"{status:12s} | {rd['human_approver']:12s} | "
                      f"匹配 {match_str}")
            print()

        # ── 核心验证 ──
        print(f"  {BOLD}核心验证结果{RESET}")
        print(f"  {'='*50}")

        # 轮 4: Gate3 回归检测
        r4_rd, r4_result, r4_match, r4_detail = all_results[3]
        r4_passed = r4_result.get("passed", True)
        r4_failed_at = r4_result.get("failed_at", "")
        print(f"  {'轮 4 Gate3 回归检测':30s}: ", end="")
        if not r4_passed and "gate3" in r4_failed_at.lower():
            print(f"{GREEN}✅ 检测成功{RESET} (failed_at={r4_failed_at})")
        else:
            print(f"{RED}❌ 未触发{RESET} (passed={r4_passed}, failed_at={r4_failed_at})")

        # 轮 7: Gate6 质量评估
        r7_rd, r7_result, r7_match, r7_detail = all_results[6]
        r7_passed = r7_result.get("passed", True)
        r7_failed_at = r7_result.get("failed_at", "")
        print(f"  {'轮 7 Gate6 质量评估':30s}: ", end="")
        if not r7_passed and "gate6" in r7_failed_at.lower():
            print(f"{GREEN}✅ 拒绝成功{RESET} (failed_at={r7_failed_at})")
        else:
            print(f"{RED}❌ 未触发{RESET} (passed={r7_passed}, failed_at={r7_failed_at})")

        # 轮 5: 恢复验证
        r5_passed = all_results[4][1].get("passed", False)
        print(f"  {'轮 5 恢复通过':30s}: ", end="")
        print(f"{GREEN}✅ 恢复正常{RESET}" if r5_passed else f"{RED}❌ 未恢复{RESET}")

        # 轮 8: 恢复验证
        r8_passed = all_results[7][1].get("passed", False)
        print(f"  {'轮 8 恢复通过':30s}: ", end="")
        print(f"{GREEN}✅ 恢复正常{RESET}" if r8_passed else f"{RED}❌ 未恢复{RESET}")

        # 轮 9: 最终验收
        r9_passed = all_results[8][1].get("passed", False)
        print(f"  {'轮 9 最终验收':30s}: ", end="")
        print(f"{GREEN}✅ 通过{RESET}" if r9_passed else f"{RED}❌ 失败{RESET}")

        print()

        # ── 审批节点统计 ──
        print(f"  {BOLD}审批节点统计{RESET}")
        print(f"  {'='*50}")
        approvers = {}
        for rd, result, _, _ in all_results:
            app = rd["human_approver"]
            if app not in approvers:
                approvers[app] = {"total": 0, "passed": 0}
            approvers[app]["total"] += 1
            if result.get("passed", False):
                approvers[app]["passed"] += 1

        for app, counts in sorted(approvers.items()):
            rate = counts["passed"] / counts["total"] * 100
            print(f"  {app:15s}: {counts['passed']}/{counts['total']} 通过 ({rate:.0f}%)")

        print()

        # ── Agent 基线统计 ──
        print(f"  {BOLD}Agent 基线演化{RESET}")
        print(f"  {'='*50}")
        agents = {}
        for rd, result, _, _ in all_results:
            ag = rd["agent"]
            if ag not in agents:
                agents[ag] = {"PRODUCTION": 0, "REJECTED": 0}
            st = str(result.get("status", "")).lower()
            if st == "production":
                agents[ag]["PRODUCTION"] += 1
            else:
                agents[ag]["REJECTED"] += 1

        for ag, counts in sorted(agents.items()):
            total = counts["PRODUCTION"] + counts["REJECTED"]
            print(f"  {ag:20s}: {counts['PRODUCTION']}/{total} PRODUCTION, "
                  f"{counts['REJECTED']} REJECTED")

        print()

        # ── 整体 pass/fail 统计 ──
        print(f"  {BOLD}整体统计{RESET}")
        total = len(all_results)
        passed_count = sum(1 for _, r, _, _ in all_results if r.get("passed", False))
        prod_count = sum(1 for _, r, _, _ in all_results if str(r.get("status", "")).lower() == "production")
        rejected_count = total - prod_count
        ipa = sum(1 for _, r, m, _ in all_results if m)
        print(f"  总轮数: {total} | PRODUCTION: {prod_count} | REJECTED: {rejected_count}")
        print(f"  预期匹配: {ipa}/{total}")
        print()

        # ── Gate7 计划一致性统计 ──
        print(f"  {BOLD}Gate7 计划 vs 执行一致性{RESET}")
        print(f"  {'='*50}")
        gate7_on_plan = 0
        gate7_warn = 0
        gate7_off_plan = 0
        gate7_skipped = 0
        for rd, result, _, _ in all_results:
            for g in result.get("gates", []):
                gn = g.get("gate_name", g.get("gate", ""))
                if "gate7" in str(gn).lower():
                    det = g.get("details", {})
                    if det.get("skipped"):
                        gate7_skipped += 1
                    else:
                        devs = det.get("deviations", [])
                        has_critical = any(d.get("severity") == "critical" for d in devs)
                        has_warning = any(d.get("severity") == "warning" for d in devs)
                        if has_critical:
                            gate7_off_plan += 1
                        elif has_warning:
                            gate7_warn += 1
                        else:
                            gate7_on_plan += 1
                    break

        print(f"  ✅ 按计划执行:    {gate7_on_plan}")
        if gate7_warn:
            print(f"  {YELLOW}⚠️  部分偏离(记录): {gate7_warn}{RESET}")
        if gate7_off_plan:
            print(f"  {RED}❌ 偏离计划(记录): {gate7_off_plan}{RESET}")
        if gate7_skipped:
            print(f"  ➖ 跳过(无计划):  {gate7_skipped}")
        print(f"  Gate7 模式: observe（只记录不阻断，可通过 config 改为 enforce）")
        print()

        if all_passed:
            print(f"  {GREEN}{BOLD}✅ 全部 9 轮与预期匹配！{RESET}")
        else:
            print(f"  {RED}{BOLD}❌ 部分轮次与预期不匹配，详见上方{RESET}")

        print()

    finally:
        # ── 4. 清理 ──
        print(f"{BOLD}[清理] 恢复配置 + 停止 server{RESET}")
        print("-" * 40)
        restore_config()
        if server_proc and server_proc.poll() is None:
            server_proc.terminate()
            server_proc.wait(timeout=5)
            print(f"  Server stopped (PID {server_proc.pid})")
        print()


if __name__ == "__main__":
    main()