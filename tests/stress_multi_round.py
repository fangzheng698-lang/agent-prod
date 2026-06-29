"""Multi-Round Evolutionary Stress Test — 验证飞轮基线与回归检测。

场景设计:
  "资深架构师 agent" (architect-v2) 提交 5 轮架构评审，质量逐轮变化：

  轮 1 (PRODUCTION):  高质量，建立基线 ✅
  轮 2 (PRODUCTION):  高质量，强化基线 ✅
  轮 3 (PRODUCTION):  高质量，强化基线 ✅
  轮 4 (REJECTED):    低质量，触发 Gate3 回归检测 ❌
  轮 5 (PRODUCTION):  恢复高质量，回归通过 ✅

验证目标:
  1. Gate3 回归检测在累积 3 轮基线后能否检测到第 4 轮的质量下降
  2. 飞轮 baseline 自动演进 (auto_evolve_baseline)
  3. 动态基线计算 (dynamic_baseline) 基于历史 PRODUCTION 记录
  4. 修复后第 5 轮回归正常通过

依赖:
  - agent-prod 服务运行中 (http://localhost:8000)
  - 服务使用 FileRepository (config.yaml storage.backend=file)
  - 环境变量 OPENAI_API_KEY 已设置 (Gate6 评估用)

使用方法（自动）:
  python tests/stress_multi_round.py

使用方法（手动）:
  1. 确保服务已启动:  QUALITY_GATES_MODE=production agent-prod serve
  2. 运行本脚本:        python tests/stress_multi_round.py
  3. 查看结果:          agent-prod stats --agent architect-v2
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

# ── 配置路径 ──
CONFIG_PATH = Path(__file__).resolve().parent.parent / "src" / "agent_prod" / "gates" / "config.yaml"
TEST_AGENT = "architect-v2"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_round(num: int, agent: str, session_id: str, result: dict, quality: str):
    status = result.get("status", "?")
    passed = result.get("passed", False)
    gates = len(result.get("gates", []))
    failed_at = result.get("failed_at", "")
    reason = result.get("fail_reason", "")

    icon = "✅" if passed else "❌"
    color = GREEN if passed else RED

    print(f"  {icon} {BOLD}轮 {num}: {agent}/{session_id}{RESET}")
    print(f"     quality={YELLOW}{quality}{RESET}, status={color}{status}{RESET}, "
          f"passed={passed}, gates={gates}")
    if failed_at:
        print(f"     {RED}failed_at={failed_at}{RESET}")
    if reason:
        print(f"     reason={reason[:200]}")
    print()


# ═══════════════════════════════════════════════════════════════════
#  Server Management
# ═══════════════════════════════════════════════════════════════════

def ensure_config():
    """读取配置，备份原文件，设置 storage.backend=file + 添加测试 agent."""
    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    # 备份
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if not backup_path.exists():
        backup_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
        print(f"  Config backup: {backup_path}")

    modified = False

    # 设置 storage.backend = file
    storage = config.setdefault("storage", {})
    if storage.get("backend") != "file":
        storage["backend"] = "file"
        storage["file_path"] = str(Path(__file__).resolve().parent.parent / "data" / "improvements.json")
        modified = True
        print(f"  {GREEN}storage.backend → file{RESET}")

    # 添加测试 agent 到 Gate0 observe 模式
    gate0 = config.setdefault("gates", {}).setdefault("gate0", {})
    per_agent = gate0.setdefault("per_agent", {})
    if TEST_AGENT not in per_agent:
        per_agent[TEST_AGENT] = {"mode": "observe"}
        modified = True
        print(f"  {GREEN}Added {TEST_AGENT} → Gate0 observe{RESET}")

    # 确保 gate3 动态基线开启
    gate3 = config.setdefault("gates", {}).setdefault("gate3", {})
    if not gate3.get("dynamic_baseline"):
        gate3["dynamic_baseline"] = True
        modified = True
        print(f"  {GREEN}Enabled gate3.dynamic_baseline{RESET}")
    if not gate3.get("auto_evolve_baseline"):
        gate3["auto_evolve_baseline"] = True
        modified = True
        print(f"  {GREEN}Enabled gate3.auto_evolve_baseline{RESET}")
    # baseline_min_samples=5 是默认值，但只有 3 轮 PRODUCTION 达不到。
    # 设为 3 以便 3 轮后生效
    if gate3.get("baseline_min_samples", 5) > 3:
        gate3["baseline_min_samples"] = 3
        modified = True
        print(f"  {GREEN}gate3.baseline_min_samples → 3 (确保第 4 轮触发回归){RESET}")

    if modified:
        CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
        print(f"  Config updated: {CONFIG_PATH}")
    else:
        print(f"  Config already set up correctly")
    print()


def restore_config():
    """恢复备份的配置."""
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if backup_path.exists():
        CONFIG_PATH.write_text(backup_path.read_text())
        backup_path.unlink()
        print(f"  Config restored from backup")
    else:
        print(f"  No backup found, skipping restore")


def ensure_server():
    """确保 server 以 production mode (FileRepository) 运行。"""
    # 检查已有 server
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:8000/health",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                print(f"  {GREEN}Server already running on :8000{RESET}")
                # 检查是否是 memory mode — 我们无法判断，但只要有健康响应就行
                # 但 memory mode 会丢失基线，所以需要重新启动
                print(f"  {YELLOW}Will restart server in production mode...{RESET}")
                # Stop existing server
                pid = int(subprocess.run(
                    ["lsof", "-ti", ":8000"],
                    capture_output=True, text=True
                ).stdout.strip())
                if pid:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(2)
                    print(f"  Stopped existing server (PID {pid})")
    except Exception:
        print(f"  No running server detected")

    # 启动新 server
    env = os.environ.copy()
    env["QUALITY_GATES_MODE"] = "production"
    # 确保 OPENAI_API_KEY 传入
    if "OPENAI_API_KEY" not in env:
        # 尝试从 config 的 security 段读取
        config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        api_key = config.get("security", {}).get("api_key", "")
        if api_key:
            env["OPENAI_API_KEY"] = api_key

    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_prod", "serve", "--port", "8000"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  Started server (PID {proc.pid}) with QUALITY_GATES_MODE=production")
    time.sleep(4)

    # 验证
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:8000/health",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"  {GREEN}Server health: {data.get('status')}{RESET}")
    except Exception as e:
        print(f"  {RED}Server failed to start: {e}{RESET}")
        sys.exit(1)

    return proc


# ═══════════════════════════════════════════════════════════════════
#  Agent: architect-v2 (资深架构师)
#  5 轮架构评审，质量逐轮变化
# ═══════════════════════════════════════════════════════════════════

ROUNDS = [
    {
        "num": 1,
        "session_id": "arch_v2_r1_good",
        "quality": "高质量 — 详细架构设计文档",
        "success_rate": 1.0,
        "error_rate": 0.0,
        "token_efficiency": 0.92,
        "latency_p95_ms": 1200,
        "final_response": (
            "## 微服务架构设计评审报告 v2.1\n\n"
            "### 整体评估\n"
            "本次评审了订单服务的架构设计方案，总体质量良好。\n\n"
            "### 1. 服务拆分\n"
            "- 订单服务: 独立部署，负责订单生命周期管理 ✓\n"
            "- 支付服务: 同步调用改为异步事件驱动 ✓\n"
            "- 库存服务: 预留缓存层设计 ✓\n\n"
            "### 2. 数据一致性\n"
            "- 采用 Saga 模式处理分布式事务 ✓\n"
            "- 本地消息表确保最终一致性 ✓\n"
            "- 幂等性设计覆盖所有关键接口 ✓\n\n"
            "### 3. 性能评估\n"
            "- 预估 TPS: 5000/s，峰值 12000/s\n"
            "- 数据库: 读写分离 + 分表方案\n"
            "- 缓存: Redis 集群，命中率目标 >95%\n\n"
            "### 4. 安全审查\n"
            "- 认证: OAuth 2.0 + JWT ✓\n"
            "- 鉴权: RBAC 模型 ✓\n"
            "- 数据加密: 传输层 TLS 1.3，存储层 AES-256 ✓\n\n"
            "### 结论\n"
            "架构设计符合公司技术规范，建议按评审意见优化后进入开发阶段。"
        ),
        "expected": "✅ PRODUCTION — 建立基线",
    },
    {
        "num": 2,
        "session_id": "arch_v2_r2_good",
        "quality": "高质量 — 数据库设计评审",
        "success_rate": 1.0,
        "error_rate": 0.0,
        "token_efficiency": 0.95,
        "latency_p95_ms": 1100,
        "final_response": (
            "## 数据库架构设计评审报告\n\n"
            "### 整体评估\n"
            "本次评审了用户服务的数据模型设计，方案成熟可靠。\n\n"
            "### 1. 表结构设计\n"
            "- 用户主表: 合理分表，按 user_id hash 分 64 表 ✓\n"
            "- 索引设计: 覆盖所有查询场景，无冗余索引 ✓\n"
            "- 字段类型: 使用合适的数据类型，无过度设计 ✓\n\n"
            "### 2. 迁移方案\n"
            "- 增量迁移: 双写方案，灰度切流 ✓\n"
            "- 回滚预案: 完整的回滚脚本 ✓\n"
            "- 数据校验: 迁移前后数据一致性校验 ✓\n\n"
            "### 3. 性能预估\n"
            "- 单表数据量: 约 500 万行/月\n"
            "- 查询性能: 主键查询 <5ms，索引查询 <20ms\n"
            "- 写入性能: 批量写入 1000 行/s\n\n"
            "### 4. 高可用\n"
            "- 主从复制: 一主两从，半同步复制 ✓\n"
            "- 备份策略: 每日全量 + 每小时增量 ✓\n"
            "- 容灾: 跨可用区部署 ✓\n\n"
            "### 结论\n"
            "数据库设计方案完善，评审通过。"
        ),
        "expected": "✅ PRODUCTION — 强化基线",
    },
    {
        "num": 3,
        "session_id": "arch_v2_r3_good",
        "quality": "高质量 — 安全架构评审",
        "success_rate": 1.0,
        "error_rate": 0.0,
        "token_efficiency": 0.93,
        "latency_p95_ms": 1150,
        "final_response": (
            "## 安全架构设计评审报告\n\n"
            "### 整体评估\n"
            "本次评审了 API 网关的安全架构方案，安全性较高。\n\n"
            "### 1. 认证体系\n"
            "- 多因素认证: 密码 + TOTP ✓\n"
            "- SSO 集成: 支持 SAML 2.0 和 OIDC ✓\n"
            "- Token 管理: 短寿命 access_token + 长寿命 refresh_token ✓\n\n"
            "### 2. 授权体系\n"
            "- 细粒度权限: 资源级 RBAC + ABAC 混合 ✓\n"
            "- API 鉴权: 每个接口独立鉴权 ✓\n"
            "- 频率限制: 用户级 + IP 级限流 ✓\n\n"
            "### 3. 数据安全\n"
            "- PII 数据: 自动脱敏 + 加密存储 ✓\n"
            "- 审计日志: 所有敏感操作记录 ✓\n"
            "- 数据保留: 自动清理策略 ✓\n\n"
            "### 4. 合规\n"
            "- GDPR: 数据可删除、可导出 ✓\n"
            "- SOC2: 控制点全部覆盖 ✓\n"
            "- PCI-DSS: 支付数据不落地 ✓\n\n"
            "### 结论\n"
            "安全架构评审通过，建议补充渗透测试计划。"
        ),
        "expected": "✅ PRODUCTION — 强化基线",
    },
    {
        "num": 4,
        "session_id": "arch_v2_r4_bad",
        "quality": "低质量 — 敷衍了事，回答极短",
        "success_rate": 0.55,       # 显著低于基线的 1.0
        "error_rate": 0.45,         # 显著高于基线的 0.0
        "token_efficiency": 0.30,   # 显著低于基线的 ~0.93
        "latency_p95_ms": 5000,     # 显著高于基线的 ~1150
        "final_response": (
            "架构评审完成，没发现大问题。整体设计还行，"
            "有些小细节可以优化但不影响上线。建议按时交付。"
        ),
        "expected": "❌ Gate3 回归检测拒绝 — 质量下降",
    },
    {
        "num": 5,
        "session_id": "arch_v2_r5_recover",
        "quality": "恢复高质量 — 完整评审",
        "success_rate": 0.98,
        "error_rate": 0.02,
        "token_efficiency": 0.90,
        "latency_p95_ms": 1300,
        "final_response": (
            "## 缓存架构设计评审报告\n\n"
            "### 整体评估\n"
            "本次评审了新的多级缓存设计方案，方案成熟。\n\n"
            "### 1. 缓存层级\n"
            "- L1: 本地缓存 (Caffeine)，TTL 5s ✓\n"
            "- L2: Redis 集群，TTL 300s ✓\n"
            "- L3: 数据库兜底 ✓\n\n"
            "### 2. 一致性问题\n"
            "- 更新策略: Cache-Aside + 延迟双删 ✓\n"
            "- 缓存穿透: Bloom Filter 防护 ✓\n"
            "- 缓存雪崩: 随机过期时间 + 限流 ✓\n"
            "- 缓存击穿: 互斥锁更新 ✓\n\n"
            "### 3. 性能指标\n"
            "- 预估命中率: L1 40% + L2 55% = 95% 总命中率\n"
            "- 响应时间: 命中 L1 <1ms，命中 L2 <5ms\n"
            "- QPS: 单 Redis 节点支撑 50000/s\n\n"
            "### 4. 监控\n"
            "- 命中率监控: 按接口维度上报 ✓\n"
            "- 大 Key 告警: >10KB 自动告警 ✓\n"
            "- 慢查询: >50ms 记录慢日志 ✓\n\n"
            "### 结论\n"
            "缓存设计方案评审通过，建议先灰度 10% 流量观察命中率。"
        ),
        "expected": "✅ PRODUCTION — 回归正常",
    },
]


def main():
    print()
    print("=" * 65)
    print(f"  {BOLD}agent-prod 多轮进化压力测试{RESET}")
    print(f"  Multi-Round Evolutionary Stress Test")
    print("=" * 65)
    print()
    print(f"  场景: '资深架构师' ({TEST_AGENT}) 提交 5 轮架构评审")
    print(f"  验证: 飞轮基线累积 → 回归检测 → 恢复验证")
    print(f"  依赖: FileRepository + dynamic_baseline + auto_evolve_baseline")
    print()

    # ── 1. 配置环境 ──
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

    try:
        # ── 3. 执行 5 轮 ──
        for rd in ROUNDS:
            num = rd["num"]
            sid = rd["session_id"]
            quality = rd["quality"]
            expected = rd["expected"]

            print(f"{BOLD}── 轮 {num}/{len(ROUNDS)}: {quality} ──{RESET}")
            print(f"  session_id: {sid}")
            print(f"  expected:   {expected}")
            print()

            result = trace(
                agent=TEST_AGENT,
                session_id=sid,
                version="2.0.0",
                decisions=[{
                    "decision_id": f"r{num}-d1",
                    "model": "gpt-4",
                    "prompt_tokens": 2500,
                    "completion_tokens": 1000,
                    "reasoning": f"第 {num} 轮架构评审",
                    "tool_calls": [
                        {
                            "tool_id": f"r{num}-tc-1",
                            "tool_name": "read_file",
                            "arguments": {"path": f"docs/round_{num}/design.md"},
                            "result_summary": f"第 {num} 轮设计文档内容",
                            "success": True,
                            "duration_ms": 300.0,
                        },
                        {
                            "tool_id": f"r{num}-tc-2",
                            "tool_name": "search_files",
                            "arguments": {"pattern": "TODO|FIXME|HACK", "path": f"src/round_{num}/"},
                            "result_summary": "发现若干待办项",
                            "success": True,
                            "duration_ms": 400.0,
                        },
                    ],
                }],
                current_metrics={
                    "latency_p95_ms": rd["latency_p95_ms"],
                    "success_rate": rd["success_rate"],
                    "error_rate": rd["error_rate"],
                    "token_efficiency": rd["token_efficiency"],
                    "final_response": rd["final_response"],
                },
                traffic_percentage=100,
                human_approver="tech-lead",
                declared_tools=["read_file", "search_files"],
                budget_tokens=50000,
                budget_time_ms=300000,
            )

            all_results.append((num, sid, quality, expected, result))
            print_round(num, TEST_AGENT, sid, result, quality)

            # 轮间等待，确保仓库写入完成 + 时间戳区分
            time.sleep(2)

        # ═══════════════════════════════════════════════════════════════
        #  汇总报告
        # ═══════════════════════════════════════════════════════════════

        print("=" * 65)
        print(f"  {BOLD}测试完成 — 结果汇总{RESET}")
        print("=" * 65)
        print()

        passed_rounds = 0
        expected_passed = 0
        expected_rejected = 0

        for num, sid, quality, expected, result in all_results:
            passed = result.get("passed", False)
            status = result.get("status", "?")
            failed_at = result.get("failed_at", "")

            icon = "✅" if passed else "❌"
            exp_icon = "✅" if "PRODUCTION" in expected else "❌"

            match = "✓" if (passed and "PRODUCTION" in expected) or (not passed and "REJECT" in expected) else "✗"
            match_color = GREEN if match == "✓" else RED

            print(f"  {icon} 轮 {num:2d} | {status:12s} | {failed_at or '—':20s} | "
                  f"预期 {exp_icon} | 匹配 {match_color}{match}{RESET}")
            print(f"       {CYAN}{quality}{RESET}")

            if passed:
                passed_rounds += 1
            if "PRODUCTION" in expected:
                expected_passed += 1
            else:
                expected_rejected += 1

        print()

        # 核心验证: 轮 4 是否被 Gate3 拒绝
        r4_result = all_results[3][4]
        r4_passed = r4_result.get("passed", True)
        r4_failed_at = r4_result.get("failed_at", "")

        print(f"  {BOLD}关键验证{RESET}")
        print(f"  {'='*50}")
        if not r4_passed and "gate3" in r4_failed_at.lower():
            print(f"  {GREEN}✓ Gate3 回归检测成功: 第 4 轮低质量被拒绝 (failed_at={r4_failed_at}){RESET}")
        elif not r4_passed:
            print(f"  {YELLOW}~ Gate3 未触发但被其他门拒绝: failed_at={r4_failed_at}{RESET}")
            print(f"    检查各道门的详细结果...")
        else:
            print(f"  {RED}✗ Gate3 回归检测未触发: 第 4 轮低质量意外通过{RESET}")
            print(f"    可能原因: dynamic_baseline 未生效 / baseline_min_samples 不足 / 仓库未持久化")

        # 轮 5 恢复验证
        r5_result = all_results[4][4]
        r5_passed = r5_result.get("passed", False)
        if r5_passed:
            print(f"  {GREEN}✓ 恢复验证通过: 第 5 轮高质量回归正常{RESET}")
        else:
            print(f"  {RED}✗ 恢复验证失败: 第 5 轮高质量也被拒绝{RESET}")

        print()
        print(f"  {BOLD}统计数据{RESET}")
        print(f"  总轮数: {len(all_results)}")
        print(f"  通过: {passed_rounds}/{len(all_results)}")
        print(f"  基线累积: {expected_passed} 轮 PRODUCTION")
        print(f"  回归检测: {'触发' if not r4_passed else '未触发'}")
        print(f"  恢复能力: {'正常' if r5_passed else '异常'}")
        print()

        # 输出 stats 查询命令
        print(f"  详细统计: {CYAN}agent-prod stats --agent {TEST_AGENT}{RESET}")
        print(f"  详情查询: {CYAN}agent-prod stats --detail <id>{RESET}")
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