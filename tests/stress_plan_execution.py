"""计划 vs 执行一致性测试 — 验证门禁能否发现子 agent 未按计划执行。

背景:
  主 agent 分配任务给子 agent，但子 agent 可能：
  1. 完全按计划执行 ✅
  2. 偏离计划做无关的事 ❌
  3. 只完成部分任务 ⚠️

  当前系统只有 Gate6 (checklist) 能间接评估"回答质量"，
  但没有直接的"计划 vs 实际"对比门。

本测试验证 Gate6 checklist 在"按计划执行"度量上的能力边界。

场景:
  场景 1: 子 agent 按计划完成 → 预期 PRODUCTION
  场景 2: 子 agent 偏离计划 → 预期 ?
  场景 3: 子 agent 部分完成 → 预期 ?
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

CONFIG_PATH = Path(__file__).resolve().parent.parent / "src" / "agent_prod" / "gates" / "config.yaml"
DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ═══════════════════════════════════════════════════════════════════
#  测试场景
# ═══════════════════════════════════════════════════════════════════

SCENES = [
    {
        "id": "scene1-on-plan",
        "title": "场景 1: 子 agent 按计划完成",
        "expected_plan": "实现 POST /api/orders 订单创建接口，含参数校验、数据库写入、返回订单ID",
        "final_response": (
            "POST /api/orders 接口实现完成。\n\n"
            "### 请求格式\n"
            "- Method: POST\n"
            "- Path: /api/orders\n"
            "- Body: { product_id, quantity, customer_info }\n\n"
            "### 实现内容\n"
            "- 参数校验: 必填字段检查 + 库存校验 ✓\n"
            "- 数据库写入: orders 表插入 + 状态初始化 ✓\n"
            "- 返回: 201 Created + 订单ID + 创建时间 ✓\n\n"
            "### 单元测试覆盖\n"
            "- 正常创建: 返回正确订单ID\n"
            "- 参数缺失: 返回 400 Bad Request\n"
            "- 库存不足: 返回 409 Conflict"
        ),
        "expect": "PRODUCTION",
    },
    {
        "id": "scene2-off-plan",
        "title": "场景 2: 子 agent 偏离计划（做了无关的事）",
        "expected_plan": "优化数据库查询性能，分析慢查询日志，添加合适的数据库索引",
        "final_response": (
            "我重构了整个用户界面，改用 React + TypeScript。\n\n"
            "### 前端重构内容\n"
            "- 组件树重构: 按业务模块拆分 ✓\n"
            "- 状态管理: 从 Redux 迁移到 Zustand ✓\n"
            "- UI 框架: 从 Ant Design 迁移到 Tailwind CSS ✓\n\n"
            "### 性能提升\n"
            "- 首屏加载: 从 3.2s 降到 0.8s\n"
            "- 构建体积: 减少 65%"
        ),
        "expect": "GATE6",
    },
    {
        "id": "scene3-partial",
        "title": "场景 3: 子 agent 部分完成（漏了任务）",
        "expected_plan": "实现 3 个 REST API: 1) POST /orders 创建订单 2) GET /orders/{id} 查询订单 3) DELETE /orders/{id} 删除订单",
        "final_response": (
            "订单服务 API 实现。\n\n"
            "### 已实现接口\n\n"
            "1. POST /api/orders\n"
            "   - 创建新订单\n"
            "   - 参数校验: 商品ID、数量、客户信息\n"
            "   - 返回 201 + 订单ID\n\n"
            "2. GET /api/orders/{id}\n"
            "   - 查询订单详情\n"
            "   - 返回完整订单信息\n"
            "   - 订单不存在返回 404\n\n"
            "### 待实现\n"
            "- DELETE 接口下一轮迭代再做"
        ),
        "expect": "GATE6",
    },
    {
        "id": "scene4-minimal",
        "title": "场景 4: 子 agent 回复极其敷衍（完全不执行）",
        "expected_plan": "编写单元测试覆盖用户模块，要求测试覆盖率 >= 80%",
        "final_response": "好的，已了解需求。代码我看了，没啥问题。",
        "expect": "GATE6",
    },
]


# ═══════════════════════════════════════════════════════════════════
#  环境配置
# ═══════════════════════════════════════════════════════════════════

def ensure_config():
    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if not backup_path.exists():
        backup_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))

    config.setdefault("storage", {})["backend"] = "file"
    config["storage"]["file_path"] = str(
        Path(__file__).resolve().parent.parent / "data" / "plan_exec_test.json"
    )

    gate0 = config.setdefault("gates", {}).setdefault("gate0", {})
    for name in ["architect-agent", "coder-agent"]:
        gate0.setdefault("per_agent", {}).setdefault(name, {})["mode"] = "observe"

    gate3 = config.setdefault("gates", {}).setdefault("gate3", {})
    gate3["dynamic_baseline"] = True
    gate3["auto_evolve_baseline"] = True
    gate3["baseline_min_samples"] = 5

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
    print(f"  Config written: storage=file, Gate6 api_key={'present' if dotenv_key else 'MISSING'}")


def restore_config():
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if backup_path.exists():
        CONFIG_PATH.write_text(backup_path.read_text())
        backup_path.unlink()
        print(f"  Config restored from backup")


def ensure_server():
    try:
        pid_str = subprocess.run(
            ["lsof", "-ti", ":8000"], capture_output=True, text=True
        ).stdout.strip()
        if pid_str:
            for pid in pid_str.split():
                os.kill(int(pid), signal.SIGTERM)
            time.sleep(2)
    except Exception:
        pass

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
    print(f"  Started server (PID {proc.pid})")
    time.sleep(4)

    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:8000/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"  {GREEN}Health: {data.get('status')}{RESET}")
    except Exception as e:
        print(f"  {RED}Server failed: {e}{RESET}")
        sys.exit(1)

    return proc


# ═══════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════

def build_decisions(scene: dict):
    tc_count = 4
    tool_calls = [
        {
            "tool_id": f"{scene['id']}-tc-{i}",
            "tool_name": "read_file",
            "arguments": {"path": "/tmp/src/"},
            "result_summary": f"读取源文件 {i}",
            "success": True,
            "duration_ms": 200.0,
        }
        for i in range(tc_count)
    ]
    return [{
        "decision_id": f"{scene['id']}-d1",
        "model": "gpt-4",
        "prompt_tokens": 2000,
        "completion_tokens": 1500,
        "reasoning": f"执行任务: {scene['expected_plan']}",
        "tool_calls": tool_calls,
    }]


def main():
    print()
    print("=" * 65)
    print(f"  {BOLD}计划 vs 执行一致性测试{RESET}")
    print(f"  Plan vs Execution — Gate6 能力边界验证")
    print("=" * 65)
    print()
    print(f"  问题: 主 agent 分配任务给子 agent，但子 agent 是否按计划执行？")
    print(f"  方法: 通过 Gate6 checklist 评估 final_response 是否匹配 expected_plan")
    print()

    # ── 1. 配置 ──
    print(f"{BOLD}[准备] 配置环境{RESET}{'-'*40}")
    ensure_config()
    print()

    # ── 2. 启动 server ──
    print(f"{BOLD}[准备] 启动 server{RESET}{'-'*40}")
    server_proc = ensure_server()
    print()

    all_results = []

    try:
        # ── 3. 执行 4 个场景 ──
        for scene in SCENES:
            print(f"{BOLD}── {scene['title']} ──{RESET}")
            print(f"  分配任务: {CYAN}{scene['expected_plan'][:80]}...{RESET}")
            print(f"  实际回复: {scene['final_response'][:80]}...")
            print(f"  预期: ", end="")

            exp_label = {"PRODUCTION": f"{GREEN}✅ PRODUCTION{RESET}",
                         "GATE6": f"{YELLOW}⚠️ Gate6 可能拒绝或通过{RESET}"}[scene["expect"]]
            print(exp_label)
            print()

            # 通过 current_metrics 传递 expected_plan 和 user_question
            result = trace(
                agent="coder-agent",
                session_id=scene["id"],
                version="1.0.0",
                decisions=build_decisions(scene),
                current_metrics={
                    "latency_p95_ms": 1500,
                    "success_rate": 0.95,
                    "error_rate": 0.05,
                    "token_efficiency": 0.85,
                    "final_response": scene["final_response"],
                    # 关键: 通过这几个字段让 Gate6/Gate7 了解预期任务
                    "expected_plan": scene["expected_plan"],
                    "expected_answer": scene["expected_plan"],
                    "user_question": f"请完成以下任务: {scene['expected_plan']}",
                },
                traffic_percentage=100,
                human_approver="tech-lead",
                declared_tools=["read_file"],
                budget_tokens=50000,
                budget_time_ms=300000,
                metadata={
                    "expected_plan": scene["expected_plan"],
                    "test_scene": scene["id"],
                },
            )

            passed = result.get("passed", False)
            status = result.get("status", "?")
            failed_at = result.get("failed_at", "")
            fail_reason = result.get("fail_reason", "")
            gates = result.get("gates", [])
            gate6_result = None
            for g in gates:
                if "gate6" in str(g.get("gate", "")).lower() or "gate6" in str(g.get("gate_name", "")).lower():
                    gate6_result = g
                    break

            icon = "✅" if passed else "❌"
            color = GREEN if passed else RED

            print(f"  {icon} {BOLD}结果: {color}{status}{RESET}")
            if failed_at:
                print(f"    拒绝于: {failed_at}")
                if fail_reason:
                    print(f"    原因:   {fail_reason[:200]}")

            # Gate6 详情
            if gate6_result:
                score = gate6_result.get("score") or gate6_result.get("details", {}).get("score", "?")
                print(f"    Gate6 score: {YELLOW}{score}{RESET}")
                details = gate6_result.get("details", {})
                if isinstance(details, dict) and "checklist" in details:
                    passed_items = [k for k, v in details["checklist"].items() if v]
                    failed_items = [k for k, v in details["checklist"].items() if not v]
                    if passed_items:
                        print(f"    ✓ 通过项: {', '.join(passed_items)}")
                    if failed_items:
                        print(f"    ✗ 未通过项: {YELLOW}{', '.join(failed_items)}{RESET}")
                elif isinstance(details, dict) and "items" in details:
                    items = details["items"]
                    if isinstance(items, list):
                        passed_items = [i.get("name", "?") for i in items if i.get("passed")]
                        failed_items = [i.get("name", "?") for i in items if not i.get("passed")]
                        if passed_items:
                            print(f"    ✓ 通过项: {', '.join(passed_items)}")
                        if failed_items:
                            print(f"    ✗ 未通过项: {YELLOW}{', '.join(failed_items)}{RESET}")

            all_results.append((scene, result, gate6_result))
            print()

        # ═══════════════════════════════════════════════════════════════
        #  汇总报告
        # ═══════════════════════════════════════════════════════════════

        print("=" * 65)
        print(f"  {BOLD}测试完成 — 计划 vs 执行一致性报告{RESET}")
        print("=" * 65)
        print()

        print(f"  {'场景':<35s} {'结果':<10s} {'Gate6 是否发现偏离?'}")
        print(f"  {'─'*35} {'─'*8} {'─'*25}")
        for scene, result, g6 in all_results:
            passed = result.get("passed", False)
            failed_at = result.get("failed_at", "")
            status = result.get("status", "?")
            icon = "✅" if passed else "❌"

            # 判断 Gate6 是否发现了偏离
            g6_found = "?"
            if not passed and "gate6" in failed_at.lower():
                g6_found = f"{GREEN}✅ 发现偏离{RESET}"
            elif passed:
                g6_found = f"{YELLOW}⚠️ 未发现 (通过){RESET}"
            else:
                g6_found = f"{YELLOW}⚠️ 其他门拒绝{RESET}"

            print(f"  {icon} {scene['id']:<30s} {status:<10s} {g6_found}")

        print()
        print(f"  {BOLD}结论{RESET}")
        print(f"  {'='*50}")

        # 分析: 哪些场景 Gate6 能发现偏离
        g6_caught = []
        g6_missed = []
        for scene, result, g6 in all_results:
            passed = result.get("passed", False)
            failed_at = result.get("failed_at", "")
            is_off_plan = scene["expect"] == "GATE6"
            if is_off_plan and not passed and "gate6" in failed_at.lower():
                g6_caught.append(scene["id"])
            elif is_off_plan and passed:
                g6_missed.append(scene["id"])

        if g6_caught:
            print(f"  {GREEN}✅ Gate6 成功发现的偏离:{RESET}")
            for sid in g6_caught:
                s = next(s for s in SCENES if s["id"] == sid)
                print(f"     - {s['title']}")
        if g6_missed:
            print(f"  {RED}❌ Gate6 未能发现的偏离:{RESET}")
            for sid in g6_missed:
                s = next(s for s in SCENES if s["id"] == sid)
                print(f"     - {s['title']}")
        print()

        print(f"  {BOLD}能力边界分析{RESET}")
        print(f"  Gate6 checklist 评估器:")
        print(f"    - 对完全偏离（做无关的事）: {'可能发现' if 'scene2-off-plan' in g6_caught else '未验证'}")
        print(f"    - 对部分完成（漏任务）:     {'可能发现' if 'scene3-partial' in g6_caught else '未验证'}")
        print(f"    - 对敷衍回复:               {'可能发现' if 'scene4-minimal' in g6_caught else '未验证'}")
        print()
        print(f"  如需精确的'计划 vs 执行'对比，需要:")
        print(f"    - 增强 Gate6 checklist: 新增 follows_plan 维度")
        print(f"    - 或新增 Gate7: 专门对比 expected_plan vs final_response")
        print()

    finally:
        print(f"{BOLD}[清理] 恢复配置 + 停止 server{RESET}{'-'*40}")
        restore_config()
        if server_proc and server_proc.poll() is None:
            server_proc.kill()
            server_proc.wait(timeout=3)
            print(f"  Server stopped")
        print()


if __name__ == "__main__":
    main()