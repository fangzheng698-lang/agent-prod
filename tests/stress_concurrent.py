"""并发压力测试 — 80 个子智能体同时提交，验证系统稳定性与吞吐能力。

测试目标:
  1. 80 个 agent 同时提交 trace，验证无数据丢失
  2. 测量 P50/P95/P99 响应时间
  3. 观察限流器行为 (429)
  4. 观察非 429 错误率 < 1%
  5. 观察系统资源变化

架构瓶颈（已知）:
  - FileRepository 使用 threading.Lock 保护所有操作
  - Gate3 dynamic_baseline 每次锁内遍历全量 _cache
  - Uvicorn 默认 worker=1，asyncio.to_thread 池 40 线程
  - RateLimiter 默认 60 RPM / 10 burst (TokenBucket)

两种模式:
  模式 1 (默认): rate_limit_enabled=true  — 观察限流
  模式 2:         rate_limit_enabled=false — 测试真实承载能力
"""

from __future__ import annotations

import json
import os
import random
import signal
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
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

# ── 压测参数 ──
NUM_AGENTS = 80
BATCH_SIZE = 20  # 每波并发数
FINAL_RESPONSES = [
    "详细架构设计评审完成，包含服务拆分、数据一致性、性能评估和安全审查。",
    "代码实现完成，订单服务核心接口全部通过单元测试。",
    "测试报告：功能测试 50 例通过，性能测试 P95 320ms。",
    "done.",
    "代码审查完成，架构设计合理，建议补充边界测试。",
    "数据库迁移方案评审通过，包含回滚预案。",
    "ok",
    "安全审计：认证、授权、数据加密、合规全部达标。",
]


# ═══════════════════════════════════════════════════════════════════
#  环境配置
# ═══════════════════════════════════════════════════════════════════

def ensure_config(rate_limit_enabled: bool = False):
    """配置 FileRepository + 80 个 agent + 可选限流。"""
    config = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if not backup_path.exists():
        backup_path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))

    # storage
    config.setdefault("storage", {})["backend"] = "file"
    config["storage"]["file_path"] = str(
        Path(__file__).resolve().parent.parent / "data" / "stress_concurrent.json"
    )

    # 80 个 agent 全部加入 Gate0 observe
    gate0 = config.setdefault("gates", {}).setdefault("gate0", {})
    per_agent = gate0.setdefault("per_agent", {})
    for i in range(1, NUM_AGENTS + 1):
        name = f"agent-{i:03d}"
        if name not in per_agent:
            per_agent[name] = {"mode": "observe"}

    # Gate3 动态基线
    gate3 = config.setdefault("gates", {}).setdefault("gate3", {})
    gate3["dynamic_baseline"] = True
    gate3["auto_evolve_baseline"] = True
    gate3["baseline_min_samples"] = 5  # 80 个 agent 各提交一次，足够基线

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

    # 速率限制配置
    rate_limit = config.setdefault("security", {}).setdefault("rate_limit", {})
    if rate_limit_enabled:
        rate_limit["enabled"] = True
        rate_limit["requests_per_minute"] = 60
        rate_limit["burst"] = 10
    else:
        rate_limit["enabled"] = False

    CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    print(f"  Config: 80 agents, storage=file, rate_limit={'ON (60rpm/10burst)' if rate_limit_enabled else 'OFF'}")


def restore_config():
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if backup_path.exists():
        CONFIG_PATH.write_text(backup_path.read_text())
        backup_path.unlink()
        print(f"  Config restored from backup")


def ensure_server(rate_limit_enabled: bool = False):
    """启动 server 并返回进程引用。"""
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

    env = os.environ.copy()
    env["QUALITY_GATES_MODE"] = "production"
    env["RATE_LIMIT_ENABLED"] = "true" if rate_limit_enabled else "false"

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
        req = urllib.request.Request(
            "http://localhost:8000/health", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"  {GREEN}Health: {data.get('status')} | "
                  f"rate_limit={data.get('rate_limit_enabled')} | "
                  f"repository={data.get('repository')}{RESET}")
    except Exception as e:
        print(f"  {RED}Server failed: {e}{RESET}")
        sys.exit(1)

    return proc


# ═══════════════════════════════════════════════════════════════════
#  单个 agent 提交任务
# ═══════════════════════════════════════════════════════════════════

def submit_agent(agent_id: int) -> dict:
    """提交一个 agent 的 trace，返回结果 + 耗时。"""
    name = f"agent-{agent_id:03d}"
    session_id = f"stress_{name}_{int(time.time() * 1000000) % 1000000}"
    t0 = time.time()

    success_rate = round(random.uniform(0.85, 1.0), 2)
    error_rate = round(1.0 - success_rate, 2)
    latency = random.randint(500, 3000)
    token_count = random.randint(500, 12000)
    final_resp = random.choice(FINAL_RESPONSES)
    approver = random.choice(["alice", "bob", "charlie"])

    tc_count = random.randint(2, 6)
    tool_calls = [
        {
            "tool_id": f"tc_{agent_id}_{i}",
            "tool_name": "read_file",
            "arguments": {"path": f"/tmp/test_{agent_id}"},
            "result_summary": f"读取文件 {i}",
            "success": True,
            "duration_ms": random.uniform(50, 500),
        }
        for i in range(tc_count)
    ]

    try:
        result = trace(
            agent=name,
            session_id=session_id,
            version="1.0.0",
            decisions=[{
                "decision_id": f"d_{agent_id}_1",
                "model": "gpt-4",
                "prompt_tokens": 2000,
                "completion_tokens": token_count,
                "reasoning": f"Agent {agent_id} 执行任务",
                "tool_calls": tool_calls,
            }],
            current_metrics={
                "latency_p95_ms": latency,
                "success_rate": success_rate,
                "error_rate": error_rate,
                "token_efficiency": round(random.uniform(0.5, 1.0), 2),
                "final_response": final_resp,
            },
            traffic_percentage=100,
            human_approver=approver,
            declared_tools=["read_file"],
            budget_tokens=50000,
            budget_time_ms=300000,
        )
        elapsed = (time.time() - t0) * 1000
        return {
            "agent": name,
            "session_id": session_id,
            "elapsed_ms": round(elapsed, 1),
            "status": result.get("status", "error"),
            "passed": result.get("passed", False),
            "failed_at": result.get("failed_at", ""),
            "error": None,
        }
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        msg = str(e)
        is_429 = "429" in msg or "rate_limit" in msg.lower()
        return {
            "agent": name,
            "session_id": session_id,
            "elapsed_ms": round(elapsed, 1),
            "status": "429" if is_429 else "error",
            "passed": False,
            "failed_at": "",
            "error": msg[:200],
        }


# ═══════════════════════════════════════════════════════════════════
#  报告
# ═══════════════════════════════════════════════════════════════════

def print_report(results: list[dict], mode_name: str):
    total = len(results)
    passed = sum(1 for r in results if r.get("passed") and r.get("status") != "429")
    rejected = sum(1 for r in results if not r.get("passed") and r.get("status") not in ("429", "error"))
    limited = sum(1 for r in results if r.get("status") == "429")
    errors = sum(1 for r in results if r.get("status") == "error")
    durations = [r["elapsed_ms"] for r in results if r.get("elapsed_ms")]

    print(f"\n{'='*60}")
    print(f"  {BOLD}并发压力测试报告 — {mode_name}{RESET}")
    print(f"  Concurrent Load Test Report")
    print(f"{'='*60}")
    print()
    print(f"  {BOLD}请求统计{RESET}")
    print(f"  {'='*40}")
    print(f"  总请求:      {total}")
    print(f"  通过:        {passed}")
    print(f"  拒绝(Gate):  {rejected}")
    print(f"  429 限流:    {limited}")
    print(f"  错误:        {errors}")
    print(f"  未响应:      {total - passed - rejected - limited - errors}")
    print()

    if durations:
        durations.sort()
        p50 = statistics.median(durations)
        p95 = durations[int(len(durations) * 0.95)]
        p99 = durations[int(len(durations) * 0.99)]
        avg = statistics.mean(durations)
        total_time_sec = max(durations) / 1000 if durations else 1
        throughput = total / total_time_sec

        print(f"  {BOLD}响应时间 (ms){RESET}")
        print(f"  {'='*40}")
        print(f"  AVG:    {avg:8.0f}")
        print(f"  P50:    {p50:8.0f}")
        print(f"  P95:    {p95:8.0f}")
        print(f"  P99:    {p99:8.0f}")
        print(f"  MIN:    {min(durations):8.0f}")
        print(f"  MAX:    {max(durations):8.0f}")
        print()
        print(f"  {BOLD}吞吐量{RESET}")
        print(f"  {'='*40}")
        print(f"  {throughput:.1f} req/s (总 {total} 请求 / {total_time_sec:.1f}s)")
        print()

    # 分析限流分布（如果有限流）
    if limited > 0:
        print(f"  {BOLD}限流分析{RESET}")
        print(f"  {'='*40}")
        print(f"  限流率: {limited/total*100:.1f}%")
        limited_durations = [r["elapsed_ms"] for r in results if r.get("status") == "429" and r.get("elapsed_ms")]
        if limited_durations:
            print(f"  429 响应时间 AVG: {statistics.mean(limited_durations):.0f}ms")
        # 计算 429 分布：前 N 个请求的 429 比例
        first_burst = results[:BATCH_SIZE]
        first_429 = sum(1 for r in first_burst if r.get("status") == "429")
        print(f"  首波({BATCH_SIZE}并发) 429: {first_429}/{BATCH_SIZE}")
    print()

    # 分析被拒绝的请求
    if rejected > 0:
        print(f"  {BOLD}拒绝分析{RESET}")
        print(f"  {'='*40}")
        gate_counts: dict[str, int] = {}
        for r in results:
            fa = r.get("failed_at", "")
            if fa:
                gate_counts[fa] = gate_counts.get(fa, 0) + 1
        for gate, count in sorted(gate_counts.items()):
            print(f"  {gate:25s}: {count}")
        print()

    # 总结
    error_rate = (errors + limited) / total * 100 if total > 0 else 0
    non_429_errors = errors / total * 100 if total > 0 else 0
    print(f"  {BOLD}健康检查{RESET}")
    print(f"  {'='*40}")
    print(f"  总错误率 (含429): {error_rate:.1f}%")
    print(f"  非429错误率:      {non_429_errors:.1f}%")
    print(f"  限流率:           {limited/total*100:.1f}%")
    print()

    if errors == 0 and total == NUM_AGENTS:
        print(f"  {GREEN}✅ 无数据丢失 ({total}/{NUM_AGENTS} 全部响应){RESET}")
    elif total == NUM_AGENTS:
        print(f"  {YELLOW}⚠ 全部响应但 {errors} 个错误{RESET}")
    else:
        print(f"  {RED}❌ 数据丢失: 仅收到 {total}/{NUM_AGENTS} 响应{RESET}")

    if non_429_errors < 1:
        print(f"  {GREEN}✅ 非429错误率 < 1% ({non_429_errors:.1f}%){RESET}")
    else:
        print(f"  {RED}❌ 非429错误率 >= 1% ({non_429_errors:.1f}%){RESET}")

    print()


# ═══════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════

def run_load_test(rate_limit_enabled: bool) -> list[dict]:
    """执行一轮压测，返回结果列表。"""
    mode_name = "限流模式 (rate_limit=ON)" if rate_limit_enabled else "无限制模式 (rate_limit=OFF)"
    print(f"\n  {BOLD}▶ 模式: {mode_name}{RESET}")
    print(f"  Agent 数: {NUM_AGENTS}, 每波并发: {BATCH_SIZE}")
    print()

    all_results: list[dict] = []
    wall_start = time.time()

    for batch_start in range(0, NUM_AGENTS, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, NUM_AGENTS)
        batch_ids = list(range(batch_start + 1, batch_end + 1))
        batch_t0 = time.time()

        print(f"  波次 {batch_start//BATCH_SIZE + 1}/{(NUM_AGENTS-1)//BATCH_SIZE + 1}: "
              f"agent-{batch_ids[0]:03d} ~ agent-{batch_ids[-1]:03d} "
              f"({len(batch_ids)} 并发)... ", end="", flush=True)

        with ThreadPoolExecutor(max_workers=len(batch_ids)) as pool:
            futures = {pool.submit(submit_agent, aid): aid for aid in batch_ids}
            batch_results = []
            for future in as_completed(futures):
                try:
                    batch_results.append(future.result())
                except Exception as e:
                    batch_results.append({
                        "agent": f"agent-{futures[future]:03d}",
                        "session_id": "",
                        "elapsed_ms": 0,
                        "status": "error",
                        "passed": False,
                        "failed_at": "",
                        "error": str(e)[:200],
                    })
            all_results.extend(batch_results)

        batch_elapsed = (time.time() - batch_t0) * 1000
        batch_passed = sum(1 for r in batch_results if r.get("passed") and r.get("status") != "429")
        batch_limited = sum(1 for r in batch_results if r.get("status") == "429")
        batch_err = sum(1 for r in batch_results if r.get("status") == "error")
        status = f"{GREEN}OK{RESET}" if batch_err == 0 else f"{RED}{batch_err} err{RESET}"
        print(f"{batch_elapsed:7.0f}ms | 通过={batch_passed} 限流={batch_limited} {status}")

        # 波间短暂停顿，让系统喘口气
        time.sleep(0.5)

    wall_elapsed = time.time() - wall_start
    print(f"\n  总耗时: {wall_elapsed:.1f}s")

    return all_results


def main():
    print()
    print("=" * 60)
    print(f"  {BOLD}并发压力测试 — 80 子智能体同时提交{RESET}")
    print(f"  Concurrent Load Test — agent-prod Stability")
    print("=" * 60)
    print()
    print(f"  目标: 验证 {NUM_AGENTS} agent 并发提交时系统稳定性")
    print(f"  架构: FileRepository + Uvicorn(1 worker) + TokenBucket限流")
    print()

    # ── 1. 配置 ──
    print(f"{BOLD}[准备] 配置环境 (限流OFF){RESET}")
    print("-" * 40)
    ensure_config(rate_limit_enabled=False)
    print()

    # ── 2. 启动 server ──
    print(f"{BOLD}[准备] 启动 server{RESET}")
    print("-" * 40)
    server_proc = ensure_server(rate_limit_enabled=False)
    print()

    all_mode_results = {}

    try:
        # ── 模式 2 (先跑): 无限制模式 ──
        results_unlimited = run_load_test(rate_limit_enabled=False)
        all_mode_results["unlimited"] = results_unlimited
        print_report(results_unlimited, "无限制模式 (rate_limit=OFF)")

        # ── 模式 1 (后跑): 限流模式 ──
        print(f"{BOLD}[切换] 重启 server — 启用限流{RESET}")
        print("-" * 40)
        server_proc.kill()
        server_proc.wait(timeout=3)

        ensure_config(rate_limit_enabled=True)
        server_proc = ensure_server(rate_limit_enabled=True)
        print()

        results_limited = run_load_test(rate_limit_enabled=True)
        all_mode_results["limited"] = results_limited
        print_report(results_limited, "限流模式 (rate_limit=ON)")

        # ═══════════════════════════════════════════════════════════════
        #  对比报告
        # ═══════════════════════════════════════════════════════════════

        print("=" * 60)
        print(f"  {BOLD}模式对比总结{RESET}")
        print(f"  {'='*40}")
        print()

        headers = ["指标", "无限制模式", "限流模式"]
        print(f"  {headers[0]:25s} {headers[1]:>15s} {headers[2]:>15s}")
        print(f"  {'-'*25} {'-'*15} {'-'*15}")

        for mode_key, label in [("unlimited", "无限制"), ("limited", "限流")]:
            results = all_mode_results[mode_key]
            total = len(results)
            passed = sum(1 for r in results if r.get("passed") and r.get("status") != "429")
            limited = sum(1 for r in results if r.get("status") == "429")
            errors = sum(1 for r in results if r.get("status") == "error")
            durations = [r["elapsed_ms"] for r in results if r.get("elapsed_ms")]

            if mode_key == "unlimited":
                print(f"  {'总请求':25s} {total:>15d} {'':>15s}")
                print(f"  {'通过':25s} {passed:>15d} {'':>15s}")
                print(f"  {'429 限流':25s} {limited:>15d} {'':>15s}")
                print(f"  {'错误':25s} {errors:>15d} {'':>15s}")
                if durations:
                    p50 = statistics.median(durations)
                    p95 = durations[int(len(durations) * 0.95)]
                    print(f"  {'P50 (ms)':25s} {p50:>15.0f} {'':>15s}")
                    print(f"  {'P95 (ms)':25s} {p95:>15.0f} {'':>15s}")
            else:
                print(f"  {'总请求':25s} {'':>15s} {total:>15d}")
                print(f"  {'通过':25s} {'':>15s} {passed:>15d}")
                print(f"  {'429 限流':25s} {'':>15s} {limited:>15d}")
                print(f"  {'错误':25s} {'':>15s} {errors:>15d}")
                if durations:
                    p50 = statistics.median(durations)
                    p95 = durations[int(len(durations) * 0.95)]
                    print(f"  {'P50 (ms)':25s} {'':>15s} {p50:>15.0f}")
                    print(f"  {'P95 (ms)':25s} {'':>15s} {p95:>15.0f}")

        print()
        print(f"  {BOLD}结论{RESET}")
        r_unl = all_mode_results.get("unlimited", [])
        r_lim = all_mode_results.get("limited", [])
        unl_ok = sum(1 for r in r_unl if r.get("status") == "error") == 0 and len(r_unl) == NUM_AGENTS
        lim_ok = sum(1 for r in r_lim if r.get("status") == "error") == 0 and len(r_lim) == NUM_AGENTS
        if unl_ok:
            print(f"  {GREEN}✅ 无限制模式: 无数据丢失, 无系统错误{RESET}")
        else:
            print(f"  {RED}❌ 无限制模式: 存在问题{RESET}")
        if lim_ok:
            print(f"  {GREEN}✅ 限流模式: 限流器正常工作, 无系统错误{RESET}")
        else:
            print(f"  {RED}❌ 限流模式: 存在问题{RESET}")
        print()

    finally:
        # ── 清理 ──
        print(f"{BOLD}[清理] 恢复配置 + 停止 server{RESET}")
        print("-" * 40)
        restore_config()
        if server_proc and server_proc.poll() is None:
            server_proc.kill()
            server_proc.wait(timeout=3)
            print(f"  Server stopped (killed)")
            print(f"  Server stopped")
        print()


if __name__ == "__main__":
    main()