"""渐进式偏离压力测试 — 模拟单一智能体长时间运行的逐渐退化。

场景:
  同一个 agent 持续运行 30 轮，从高质量逐渐退化到完全偏离，
  恢复后再次退化。验证各个 Gate 对"渐进式偏离"的灵敏度差异。

阶段划分:
  阶段 1 (轮 1-5):   强基线期   — 高质量回复，建立 Gate3 基线
  阶段 2 (轮 6-9):   渐退化期   — 回复变短，覆盖度下降
  阶段 3 (轮 10-12): 偏离期     — 简短回复，关键词覆盖 <30%
  阶段 4 (轮 13-15): 严重偏离期 — 敷衍回复，关键词覆盖 <15%
  阶段 5 (轮 16-18): 完全放弃期 — 极简回复，无工具调用
  阶段 6 (轮 19-22): 恢复期     — 高质量回复恢复
  阶段 7 (轮 23-25): 稳定期     — 中等质量
  阶段 8 (轮 26-30): 再次退化期 — 再次偏离

验证目标:
  1. Gate7 observe 能最早发现偏离趋势（关键词覆盖率下降）
  2. Gate3 在数值指标退化时立即触发
  3. Gate6 通过 LLM 判断偏离，可能有延迟
  4. 恢复后各 Gate 正确放行
  5. 再次退化时门禁再次触发
"""
from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from agent_prod import trace

# ── yaml compatibility shim (venv may have an incomplete yaml) ──
try:
    yaml_safe_load = yaml.safe_load
    yaml_dump = yaml.dump
except AttributeError:
    import json
    def yaml_safe_load(s: str) -> dict:
        return json.loads(s) if s else {}
    def yaml_dump(data, **kwargs) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

# ── 路径 ──
CONFIG_PATH = Path(__file__).resolve().parent.parent / "src" / "agent_prod" / "gates" / "config.yaml"
DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

# ── 固定计划（30 轮不变） ──
EXPECTED_PLAN = (
    "Refactor the payment processing pipeline to support multi-currency: "
    "update currency conversion module, add FX rate caching, "
    "implement multi-currency settlement, add currency routing rules, "
    "update transaction logging"
)

# ── 回复模板片段 ──

ON_TOPIC_FRAGMENTS = [
    "Updated the currency conversion module to support 14 currency pairs with real-time rates from FX provider.",
    "Added FX rate caching with Redis, TTL of 60 seconds, fallback to last-known rate on provider failure.",
    "Implemented multi-currency settlement engine that converts all trades to base currency (USD) at end of day.",
    "Added currency routing rules: USD/EUR/GBP go direct, others go through intermediate conversion.",
    "Updated transaction logging schema to include currency_code, fx_rate_used, and settlement_currency fields.",
    "The conversion module now handles 14 currency pairs with sub-millisecond processing time.",
    "FX rate caching reduced provider API calls by 95%, with automatic refresh on stale rates.",
    "Settlement engine supports batch processing with full rollback on any transaction failure.",
    "Routing rules are now configurable via YAML without code changes or redeployment.",
    "Transaction logs capture full currency audit trail for compliance reporting.",
]

OFF_TOPIC_FRAGMENTS = [
    "Refactored the entire frontend from Vue to React with TypeScript and Redux Toolkit.",
    "Set up GitHub Actions CI/CD pipeline with automated testing, linting, and deployment to staging.",
    "Migrated the infrastructure to Kubernetes with Helm charts and ArgoCD for GitOps.",
    "Rewrote the authentication module to support OAuth 2.0 and OpenID Connect with SSO.",
    "Added WebSocket support for real-time notifications using Socket.io with room-based broadcasting.",
    "Optimized database queries by adding composite indexes on the user_actions table, reducing query time by 80%.",
    "Replaced the logging framework with structured JSON logging and centralized log aggregation.",
    "Implemented a feature flag system using LaunchDarkly for gradual rollouts and A/B testing.",
    "Upgraded all npm dependencies to latest versions and fixed all breaking changes across the codebase.",
    "Added end-to-end tests with Playwright covering all main user flows with visual regression testing.",
]

MINIMAL_RESPONSES = [
    "Done.",
    "Looks fine to me.",
    "Code reviewed, approved.",
    "All tests pass, ready to ship.",
    "Updated. Please review.",
    "Done, no issues found.",
    "Looks good, merged.",
    "Finished the changes.",
    "All good.",
    "Done.",
]


# ═══════════════════════════════════════════════════════════════════
#  30 轮参数定义
# ═══════════════════════════════════════════════════════════════════

ROUND_PARAMS = [
    # ── Phase 1: 强基线期 (rounds 1-5) ──
    {"num": 1,  "phase": "baseline",  "style": "thorough",   "kw_match": 0.80, "tool_calls": 11, "latency": 1100, "success_rate": 0.99, "error_rate": 0.01, "token_eff": 0.92, "tokens": 5000, "expect": "PRODUCTION"},
    {"num": 2,  "phase": "baseline",  "style": "thorough",   "kw_match": 0.75, "tool_calls": 10, "latency": 1200, "success_rate": 0.98, "error_rate": 0.02, "token_eff": 0.90, "tokens": 4500, "expect": "PRODUCTION"},
    {"num": 3,  "phase": "baseline",  "style": "thorough",   "kw_match": 0.80, "tool_calls": 12, "latency": 1150, "success_rate": 0.99, "error_rate": 0.01, "token_eff": 0.91, "tokens": 5200, "expect": "PRODUCTION"},
    {"num": 4,  "phase": "baseline",  "style": "thorough",   "kw_match": 0.75, "tool_calls": 10, "latency": 1250, "success_rate": 0.98, "error_rate": 0.02, "token_eff": 0.90, "tokens": 4800, "expect": "PRODUCTION"},
    {"num": 5,  "phase": "baseline",  "style": "thorough",   "kw_match": 0.80, "tool_calls": 11, "latency": 1180, "success_rate": 0.98, "error_rate": 0.02, "token_eff": 0.91, "tokens": 4900, "expect": "PRODUCTION"},
    # ── Phase 2: 渐退化期 (rounds 6-9) ──
    {"num": 6,  "phase": "waning",    "style": "adequate",   "kw_match": 0.55, "tool_calls": 7,  "latency": 1600, "success_rate": 0.95, "error_rate": 0.05, "token_eff": 0.82, "tokens": 3200, "expect": "PRODUCTION"},
    {"num": 7,  "phase": "waning",    "style": "adequate",   "kw_match": 0.50, "tool_calls": 6,  "latency": 1800, "success_rate": 0.93, "error_rate": 0.07, "token_eff": 0.78, "tokens": 2800, "expect": "GATE6_FAIL"},
    {"num": 8,  "phase": "waning",    "style": "curt",       "kw_match": 0.40, "tool_calls": 5,  "latency": 2200, "success_rate": 0.90, "error_rate": 0.10, "token_eff": 0.65, "tokens": 2000, "expect": "PRODUCTION"},
    {"num": 9,  "phase": "waning",    "style": "curt",       "kw_match": 0.35, "tool_calls": 4,  "latency": 2500, "success_rate": 0.88, "error_rate": 0.12, "token_eff": 0.55, "tokens": 1500, "expect": "GATE6_FAIL"},
    # ── Phase 3: 偏离期 (rounds 10-12) ──
    {"num": 10, "phase": "off-plan",  "style": "dismissive", "kw_match": 0.25, "tool_calls": 3,  "latency": 3000, "success_rate": 0.82, "error_rate": 0.18, "token_eff": 0.40, "tokens": 800,  "expect": "GATE3_OR_GATE6"},
    {"num": 11, "phase": "off-plan",  "style": "dismissive", "kw_match": 0.20, "tool_calls": 2,  "latency": 3500, "success_rate": 0.78, "error_rate": 0.22, "token_eff": 0.35, "tokens": 600,  "expect": "GATE6_FAIL"},
    {"num": 12, "phase": "off-plan",  "style": "dismissive", "kw_match": 0.15, "tool_calls": 2,  "latency": 3800, "success_rate": 0.75, "error_rate": 0.25, "token_eff": 0.30, "tokens": 400,  "expect": "GATE3_REJ"},
    # ── Phase 4: 严重偏离期 (rounds 13-15) ──
    {"num": 13, "phase": "bad",       "style": "minimal",    "kw_match": 0.10, "tool_calls": 1,  "latency": 4000, "success_rate": 0.65, "error_rate": 0.35, "token_eff": 0.30, "tokens": 250,  "expect": "GATE6_FAIL"},
    {"num": 14, "phase": "bad",       "style": "minimal",    "kw_match": 0.05, "tool_calls": 1,  "latency": 4500, "success_rate": 0.60, "error_rate": 0.40, "token_eff": 0.25, "tokens": 200,  "expect": "GATE6_FAIL"},
    {"num": 15, "phase": "bad",       "style": "minimal",    "kw_match": 0.05, "tool_calls": 1,  "latency": 5000, "success_rate": 0.55, "error_rate": 0.45, "token_eff": 0.20, "tokens": 150,  "expect": "GATE6_FAIL"},
    # ── Phase 5: 完全放弃期 (rounds 16-18) ──
    {"num": 16, "phase": "abandoned", "style": "abandoned",  "kw_match": 0.0,  "tool_calls": 0,  "latency": 5500, "success_rate": 0.50, "error_rate": 0.50, "token_eff": 0.15, "tokens": 50,   "expect": "GATE6_FAIL"},
    {"num": 17, "phase": "abandoned", "style": "abandoned",  "kw_match": 0.0,  "tool_calls": 0,  "latency": 6000, "success_rate": 0.45, "error_rate": 0.55, "token_eff": 0.10, "tokens": 30,   "expect": "GATE6_FAIL"},
    {"num": 18, "phase": "abandoned", "style": "abandoned",  "kw_match": 0.0,  "tool_calls": 0,  "latency": 6500, "success_rate": 0.40, "error_rate": 0.60, "token_eff": 0.10, "tokens": 20,   "expect": "GATE6_FAIL"},
    # ── Phase 6: 恢复期 (rounds 19-22) ──
    {"num": 19, "phase": "recovery",  "style": "thorough",   "kw_match": 0.70, "tool_calls": 9,  "latency": 1200, "success_rate": 0.97, "error_rate": 0.03, "token_eff": 0.88, "tokens": 4200, "expect": "PRODUCTION"},
    {"num": 20, "phase": "recovery",  "style": "thorough",   "kw_match": 0.75, "tool_calls": 10, "latency": 1150, "success_rate": 0.98, "error_rate": 0.02, "token_eff": 0.90, "tokens": 4600, "expect": "PRODUCTION"},
    {"num": 21, "phase": "recovery",  "style": "thorough",   "kw_match": 0.70, "tool_calls": 9,  "latency": 1250, "success_rate": 0.97, "error_rate": 0.03, "token_eff": 0.89, "tokens": 4300, "expect": "PRODUCTION"},
    {"num": 22, "phase": "recovery",  "style": "adequate",   "kw_match": 0.65, "tool_calls": 8,  "latency": 1300, "success_rate": 0.96, "error_rate": 0.04, "token_eff": 0.87, "tokens": 3800, "expect": "PRODUCTION"},
    # ── Phase 7: 稳定期 (rounds 23-25) ──
    {"num": 23, "phase": "stable",    "style": "adequate",   "kw_match": 0.55, "tool_calls": 6,  "latency": 1500, "success_rate": 0.94, "error_rate": 0.06, "token_eff": 0.80, "tokens": 3000, "expect": "PRODUCTION"},
    {"num": 24, "phase": "stable",    "style": "curt",       "kw_match": 0.45, "tool_calls": 5,  "latency": 1700, "success_rate": 0.92, "error_rate": 0.08, "token_eff": 0.75, "tokens": 2200, "expect": "GATE3_OR_GATE6"},
    {"num": 25, "phase": "stable",    "style": "curt",       "kw_match": 0.40, "tool_calls": 4,  "latency": 2000, "success_rate": 0.90, "error_rate": 0.10, "token_eff": 0.70, "tokens": 1800, "expect": "GATE3_OR_GATE6"},
    # ── Phase 8: 再次退化期 (rounds 26-30) ──
    {"num": 26, "phase": "relapse",   "style": "dismissive", "kw_match": 0.20, "tool_calls": 2,  "latency": 3000, "success_rate": 0.75, "error_rate": 0.25, "token_eff": 0.35, "tokens": 500,  "expect": "GATE6_FAIL"},
    {"num": 27, "phase": "relapse",   "style": "minimal",    "kw_match": 0.10, "tool_calls": 1,  "latency": 4000, "success_rate": 0.60, "error_rate": 0.40, "token_eff": 0.25, "tokens": 200,  "expect": "GATE6_FAIL"},
    {"num": 28, "phase": "relapse",   "style": "minimal",    "kw_match": 0.05, "tool_calls": 1,  "latency": 4500, "success_rate": 0.55, "error_rate": 0.45, "token_eff": 0.20, "tokens": 150,  "expect": "GATE6_FAIL"},
    {"num": 29, "phase": "relapse",   "style": "abandoned",  "kw_match": 0.0,  "tool_calls": 0,  "latency": 5000, "success_rate": 0.50, "error_rate": 0.50, "token_eff": 0.15, "tokens": 50,   "expect": "GATE6_FAIL"},
    {"num": 30, "phase": "relapse",   "style": "abandoned",  "kw_match": 0.0,  "tool_calls": 0,  "latency": 5500, "success_rate": 0.45, "error_rate": 0.55, "token_eff": 0.10, "tokens": 30,   "expect": "GATE6_FAIL"},
]

# ═══════════════════════════════════════════════════════════════════
#  回复生成
# ═══════════════════════════════════════════════════════════════════

def build_response(style: str, kw_match: float) -> str:
    """按风格和关键词覆盖率生成回复文本。"""
    if style == "abandoned":
        return random.choice(MINIMAL_RESPONSES)

    # 选择片段
    total_fragments = {
        "thorough": 8,
        "adequate": 5,
        "curt": 3,
        "dismissive": 2,
        "minimal": 1,
    }.get(style, 3)

    n_on_topic = max(0, round(total_fragments * kw_match))
    n_off_topic = total_fragments - n_on_topic

    selected = random.sample(ON_TOPIC_FRAGMENTS, min(n_on_topic, len(ON_TOPIC_FRAGMENTS)))
    selected += random.sample(OFF_TOPIC_FRAGMENTS, min(n_off_topic, len(OFF_TOPIC_FRAGMENTS)))
    random.shuffle(selected)

    body = "\n".join(f"- {s}" for s in selected)

    if style == "thorough":
        return (
            f"## Payment Pipeline Refactoring Report\n\n"
            f"### Summary\n"
            f"Completed the multi-currency refactoring across all modules.\n\n"
            f"### Changes\n"
            f"{body}\n\n"
            f"### Metrics\n"
            f"- All unit tests passing: 142/142\n"
            f"- Integration tests: 56/56\n"
            f"- Performance: P95 latency unchanged at 320ms\n\n"
            f"### Next Steps\n"
            f"Ready for production deployment after peer review."
        )
    elif style == "adequate":
        return (
            f"## Multi-Currency Update\n\n"
            f"### Changes Made\n"
            f"{body}\n\n"
            f"All tests pass, ready for review."
        )
    elif style == "curt":
        return f"Updates done:\n{body}\n\nLooks good."
    elif style == "dismissive":
        return f"Made some changes:\n{body}"
    elif style == "minimal":
        first_line = selected[0] if selected else "Updated."
        return first_line[:150]
    return body


# ═══════════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════════

PHASE_LABELS = {
    "baseline":  f"{GREEN}强基线期{RESET}",
    "waning":    f"{YELLOW}渐退化期{RESET}",
    "off-plan":  f"{RED}偏离期{RESET}",
    "bad":       f"{RED}严重偏离期{RESET}",
    "abandoned": f"{RED}完全放弃期{RESET}",
    "recovery":  f"{GREEN}恢复期{RESET}",
    "stable":    f"{YELLOW}稳定期{RESET}",
    "relapse":   f"{RED}再次退化期{RESET}",
}

EXPECT_LABELS = {
    "PRODUCTION":     f"{GREEN}✅ PRODUCTION{RESET}",
    "GATE3_OR_GATE6": f"{RED}❌ Gate3 或 Gate6 拒绝{RESET}",
    "GATE3_REJ":      f"{RED}❌ Gate3 拒绝{RESET}",
    "GATE6_FAIL":     f"{RED}❌ Gate6 拒绝{RESET}",
}

STYLE_LABELS = {
    "thorough":   "详细",
    "adequate":   "一般",
    "curt":       "简短",
    "dismissive": "敷衍",
    "minimal":    "极简",
    "abandoned":  "放弃",
}

def style_label(s: str) -> str:
    colors = {
        "thorough": GREEN, "adequate": CYAN, "curt": YELLOW,
        "dismissive": RED, "minimal": RED, "abandoned": RED,
    }
    c = colors.get(s, RESET)
    lbl = STYLE_LABELS.get(s, s)
    return f"{c}{lbl}{RESET}"


def round_match(p: dict, result: dict) -> tuple[bool, str]:
    """检查一轮结果是否匹配预期。"""
    passed = result.get("passed", False)
    status = result.get("status", "?")
    failed_at = result.get("failed_at", "")

    exp = p["expect"]

    if exp == "PRODUCTION":
        ok = passed and str(status).lower() == "production"
        return ok, "通过" if ok else f"状态={status}, passed={passed}"
    elif exp == "GATE6_FAIL":
        ok = not passed and "gate6" in str(failed_at).lower()
        return ok, f"failed_at={failed_at}" if not ok else "Gate6 正确拒绝"
    elif exp == "GATE3_REJ":
        ok = not passed and "gate3" in str(failed_at).lower()
        return ok, f"failed_at={failed_at}" if not ok else "Gate3 正确拒绝"
    elif exp == "GATE3_OR_GATE6":
        ok = not passed and ("gate3" in str(failed_at).lower() or "gate6" in str(failed_at).lower())
        return ok, f"failed_at={failed_at}" if not ok else "Gate3/6 正确拒绝"
    else:
        return False, f"未知预期: {exp}"


def get_gate7_details(result: dict) -> dict | None:
    for g in result.get("gates", []):
        gn = g.get("gate_name", g.get("gate", ""))
        if "gate7" in str(gn).lower():
            return g.get("details", {})
    return None


def get_gate6_details(result: dict) -> dict | None:
    for g in result.get("gates", []):
        gn = g.get("gate_name", g.get("gate", ""))
        if "gate6" in str(gn).lower():
            return g.get("details", {})
    return None


# ═══════════════════════════════════════════════════════════════════
#  环境配置
# ═══════════════════════════════════════════════════════════════════

def ensure_config():
    config = yaml_safe_load(CONFIG_PATH.read_text()) or {}
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if not backup_path.exists():
        backup_path.write_text(yaml_dump(config, default_flow_style=False, allow_unicode=True))

    config.setdefault("storage", {})["backend"] = "file"
    config["storage"]["file_path"] = str(
        Path(__file__).resolve().parent.parent / "data" / "gradient_degradation.json"
    )

    gate0 = config.setdefault("gates", {}).setdefault("gate0", {})
    gate0.setdefault("per_agent", {}).setdefault("long-running-agent", {})["mode"] = "observe"

    gate3 = config.setdefault("gates", {}).setdefault("gate3", {})
    gate3["dynamic_baseline"] = True
    gate3["auto_evolve_baseline"] = True
    gate3["baseline_min_samples"] = 1
    gate3["regress_pct"] = 0.70
    gate3["perf_degradation_limit"] = 0.25

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

    CONFIG_PATH.write_text(yaml_dump(config, default_flow_style=False, allow_unicode=True))
    print(f"  Config: storage=file, Gate3 baseline_min=1, Gate6 api_key={'present' if dotenv_key else 'MISSING'}")
    return dotenv_key is not None


def restore_config():
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if backup_path.exists():
        CONFIG_PATH.write_text(backup_path.read_text())
        backup_path.unlink()


AGENT_PROD_URL = os.environ.get("AGENT_PROD_URL", "http://localhost:8765")


def ensure_server():
    # Use existing server — verify it's healthy
    try:
        import urllib.request
        req = urllib.request.Request(f"{AGENT_PROD_URL}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"  Server OK: {data.get('status')}")
    except Exception as e:
        print(f"  {RED}No server at {AGENT_PROD_URL}: {e}{RESET}")
        print(f"  Starting server...")
        env = os.environ.copy()
        env["QUALITY_GATES_MODE"] = "production"
        if DOTENV_PATH.exists():
            for line in DOTENV_PATH.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("\"'")
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_prod", "serve", "--port", "8765"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"  Started server (PID {proc.pid})")
        time.sleep(4)
        try:
            with urllib.request.urlopen(f"{AGENT_PROD_URL}/health", timeout=5) as resp:
                data = json.loads(resp.read())
                print(f"  {GREEN}Health: {data.get('status')}{RESET}")
        except Exception as e2:
            print(f"  {RED}Server failed: {e2}{RESET}")
            sys.exit(1)
        return proc

    return None

    try:
        import urllib.request
        req = urllib_request.Request(f"{AGENT_PROD_URL}/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"  {GREEN}Health: {data.get('status')}{RESET}")
    except Exception as e:
        print(f"  {RED}Server failed: {e}{RESET}")
        sys.exit(1)

    return proc


# ═══════════════════════════════════════════════════════════════════
#  decisions 生成
# ═══════════════════════════════════════════════════════════════════

def build_decisions(p: dict) -> list[dict]:
    tc_count = p["tool_calls"]
    tool_calls = [
        {
            "tool_id": f"gr-tc-{p['num']}-{i}",
            "tool_name": random.choice(["read_file", "search_files", "write_file", "patch"]),
            "arguments": {"path": f"src/pipeline/{i}/"},
            "result_summary": f"Tool call {i+1}/{tc_count}",
            "success": True,
            "duration_ms": 100.0 + (i * 30),
        }
        for i in range(tc_count)
    ]

    return [{
        "decision_id": f"gr-d-{p['num']}",
        "model": "gpt-4",
        "prompt_tokens": 2000,
        "completion_tokens": p["tokens"],
        "reasoning": f"Round {p['num']}: {p['phase']}",
        "tool_calls": tool_calls,
    }]


# ═══════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════

def main():
    os.environ["AGENT_PROD_URL"] = AGENT_PROD_URL

    print("=" * 70)
    print(f"  {BOLD}渐进式偏离压力测试{RESET}")
    print(f"  Gradient Degradation Stress Test — 30 Rounds")
    print("=" * 70)
    print()
    print(f"  Agent: long-running-agent")
    print(f"  计划: {EXPECTED_PLAN[:80]}...")
    print(f"  门禁: Gate3(回归) → Gate6(质量) → Gate7(一致性, observe)")
    print(f"  验证: 各 Gate 对渐进式偏离的灵敏度差异")
    print()

    # ── 1. 配置 ──
    print(f"{BOLD}[准备] 配置环境{RESET}")
    print("-" * 40)
    has_key = ensure_config()
    if not has_key:
        print(f"  {RED}WARNING: No OPENAI_API_KEY found — Gate6 will be skipped!{RESET}")
    print()

    # ── 2. 启动 server ──
    print(f"{BOLD}[准备] 启动 server{RESET}")
    print("-" * 40)
    server_proc = ensure_server()
    print()

    all_results = []
    all_passed = True

    # ── 检测时序记录 ──
    first_gate7_warn = None
    first_gate7_crit = None
    first_gate3_rej = None
    first_gate6_rej = None
    recovery_round = None
    relapse_round = None

    try:
        # ── 3. 执行 30 轮 ──
        for p in ROUND_PARAMS:
            phase_label = PHASE_LABELS.get(p["phase"], p["phase"])
            print(f"{BOLD}── 轮 {p['num']:2d}/30 [{phase_label}]{RESET}")
            print(f"  Style={style_label(p['style'])} "
                  f"KW={p['kw_match']*100:.0f}% Tools={p['tool_calls']} "
                  f"SR={p['success_rate']:.2f} TE={p['token_eff']:.2f} "
                  f"expect={EXPECT_LABELS.get(p['expect'], p['expect'])}")

            decision = build_decisions(p)
            final_resp = build_response(p["style"], p["kw_match"])

            result = trace(
                agent="long-running-agent",
                session_id=f"gradient_r{p['num']:03d}",
                version="1.0.0",
                decisions=decision,
                current_metrics={
                    "latency_p95_ms": p["latency"],
                    "success_rate": p["success_rate"],
                    "error_rate": p["error_rate"],
                    "token_efficiency": p["token_eff"],
                    "final_response": final_resp[:5000],
                    "expected_plan": EXPECTED_PLAN,
                    "user_question": f"请完成任务: {EXPECTED_PLAN[:100]}...",
                },
                traffic_percentage=100,
                human_approver="tech-lead",
                declared_tools=["read_file", "search_files", "write_file", "patch"],
                budget_tokens=50000,
                budget_time_ms=300000,
                metadata={"expected_plan": EXPECTED_PLAN, "round": p["num"]},
                timeout=30.0,
            )

            match, detail = round_match(p, result)
            passed = result.get("passed", False)
            status = result.get("status", "?")
            failed_at = result.get("failed_at", "")

            # Gate7 详情
            g7d = get_gate7_details(result)
            g7_info = ""
            if g7d and not g7d.get("skipped"):
                devs = g7d.get("deviations", [])
                has_crit = any(d.get("severity") == "critical" for d in devs)
                has_warn = any(d.get("severity") == "warning" for d in devs)
                if has_crit:
                    g7_info = f" {RED}🔴 Gate7: critical{RESET}"
                    for d in devs:
                        if d.get("severity") == "critical":
                            g7_info += f" [{d['type']}]"
                            break
                elif has_warn:
                    g7_info = f" {YELLOW}🟡 Gate7: warning{RESET}"
                    for d in devs:
                        if d.get("severity") == "warning":
                            g7_info += f" [{d['type']}]"
                            break
                else:
                    g7_info = f" {GREEN}🟢 Gate7: OK{RESET}"

            # Gate6 score
            g6d = get_gate6_details(result)
            g6_score = ""
            if g6d:
                score = g6d.get("score", g6d.get("raw_score", ""))
                if score != "":
                    g6_score = f" score={score}"

            # 响应摘要
            resp_preview = final_resp[:60].replace("\n", " ")

            icon = "✅" if passed else "❌"
            color = GREEN if passed else RED
            match_icon = "✓" if match else "✗"
            match_color = GREEN if match else RED

            print(f"  {icon} {color}{status}{RESET}{g6_score}{g7_info}")
            if failed_at:
                reason = result.get("fail_reason", "")
                print(f"    {RED}⛔ {failed_at}{RESET}")
                if reason:
                    print(f"    {reason[:150]}")
            print(f"    Resp: {resp_preview}...")
            print(f"    {match_color}{match_icon} {detail}{RESET}")
            print()

            all_results.append((p, result, match, detail))

            if not match:
                all_passed = False

            # ── 记录首次触发 ──
            if first_gate6_rej is None and not passed and "gate6" in str(failed_at).lower():
                first_gate6_rej = p["num"]
            if first_gate3_rej is None and not passed and "gate3" in str(failed_at).lower():
                first_gate3_rej = p["num"]
            if g7d and not g7d.get("skipped"):
                devs = g7d.get("deviations", [])
                if first_gate7_warn is None and any(d.get("severity") == "warning" for d in devs):
                    first_gate7_warn = p["num"]
                if first_gate7_crit is None and any(d.get("severity") == "critical" for d in devs):
                    first_gate7_crit = p["num"]
            if recovery_round is None and passed and p["phase"] == "recovery":
                recovery_round = p["num"]
            if relapse_round is None and not passed and p["phase"] == "relapse":
                relapse_round = p["num"]

            time.sleep(1.0)

        # ═══════════════════════════════════════════════════════════════
        #  汇总报告
        # ═══════════════════════════════════════════════════════════════

        print("=" * 70)
        print(f"  {BOLD}渐进式偏离测试 — 检测时序报告{RESET}")
        print(f"  Gradient Degradation — Detection Timeline")
        print("=" * 70)
        print()

        # 按阶段输出
        for phase_name, phase_start, phase_end in [
            ("强基线期 (R1-5)", 0, 5),
            ("渐退化期 (R6-9)", 5, 9),
            ("偏离期 (R10-12)", 9, 12),
            ("严重偏离期 (R13-15)", 12, 15),
            ("完全放弃期 (R16-18)", 15, 18),
            ("恢复期 (R19-22)", 18, 22),
            ("稳定期 (R23-25)", 22, 25),
            ("再次退化期 (R26-30)", 25, 30),
        ]:
            phase_results = all_results[phase_start:phase_end]
            prod = sum(1 for _, r, _, _ in phase_results if str(r.get("status", "")).lower() == "production")
            rej = sum(1 for _, r, _, _ in phase_results if str(r.get("status", "")).lower() == "rejected")
            print(f"  {phase_name:<22s} → {GREEN}{prod} PRODUCTION{RESET} / {RED}{rej} REJECTED{RESET}")

        print()
        print(f"  {BOLD}门禁检测延迟分析{RESET}")
        print(f"  {'='*55}")
        print(f"  {'Gate':<20s} {'首次发现':<10s} {'从退化起延迟':<15s}")
        print(f"  {'─'*20:<20s} {'─'*8:<10s} {'─'*14:<15s}")

        # 退化开始于轮 6（渐退化期）
        degradation_start = 6
        for label, first_round in [
            (f"Gate7 (warning)", first_gate7_warn),
            (f"Gate3 (reject)",  first_gate3_rej),
            (f"Gate6 (reject)",  first_gate6_rej),
            (f"Gate7 (critical)", first_gate7_crit),
        ]:
            if first_round:
                delay = first_round - degradation_start
                print(f"  {label:<20s} R{first_round:<8d} +{delay} rounds")
            else:
                print(f"  {label:<20s} {'未触发':<10s} {'—':<15s}")

        print()
        print(f"  {BOLD}恢复与再次退化{RESET}")
        print(f"  {'='*55}")
        if recovery_round:
            print(f"  恢复:   R{recovery_round} 重新 PRODUCTION")
        if relapse_round:
            print(f"  再次退化: R{relapse_round} 再次触发门禁")
        print()

        # 每轮详细
        print(f"  {BOLD}每轮明细{RESET}")
        print(f"  {'轮':<4s} {'阶段':<10s} {'风格':<8s} {'KW%':<6s} {'工具':<6s} {'结果':<12s} {'Gate7'}")
        print(f"  {'─'*4:<4s} {'─'*10:<10s} {'─'*8:<8s} {'─'*6:<6s} {'─'*6:<6s} {'─'*12:<12s} {'─'*30}")
        for p, result, _, _ in all_results:
            passed = result.get("passed", False)
            status = str(result.get("status", "?"))[:10]
            s_icon = "✅" if passed else "❌"
            g7_info = ""
            g7d = get_gate7_details(result)
            if g7d and not g7d.get("skipped"):
                devs = g7d.get("deviations", [])
                if devs:
                    sev = [d.get("severity", "info")[0] for d in devs]
                    g7_info = f"{'🔴' if 'c' in sev else '🟡' if 'w' in sev else '🟢'} {','.join(d['type'][:15] for d in devs[:2])}"
                else:
                    g7_info = "🟢 OK"
            else:
                g7_info = "⚪ skip"
            print(f"  R{p['num']:<2d} {p['phase']:<10s} {STYLE_LABELS.get(p['style'],p['style']):<8s} "
                  f"{p['kw_match']*100:<6.0f} {p['tool_calls']:<6d} {s_icon} {status:<10s} {g7_info[:40]}")

        print()

        # 整体统计
        print(f"  {BOLD}整体统计{RESET}")
        print(f"  {'='*55}")
        total = len(all_results)
        prod = sum(1 for _, r, _, _ in all_results if str(r.get("status", "")).lower() == "production")
        rej = total - prod
        print(f"  总轮数: {total} | PRODUCTION: {prod} | REJECTED: {rej}")

        # Gate7 检测分析
        g7_warn_count = 0
        g7_crit_count = 0
        g7_ok_count = 0
        for _, r, _, _ in all_results:
            g7d = get_gate7_details(r)
            if g7d and not g7d.get("skipped"):
                devs = g7d.get("deviations", [])
                if any(d.get("severity") == "critical" for d in devs):
                    g7_crit_count += 1
                elif any(d.get("severity") == "warning" for d in devs):
                    g7_warn_count += 1
                else:
                    g7_ok_count += 1
        print(f"  Gate7: 🟢 OK={g7_ok_count} 🟡 WARN={g7_warn_count} 🔴 CRIT={g7_crit_count}")
        print(f"  Gate7 observe 模式: 所有 deviation 仅记录不阻断")

        # 检测延迟总结
        print()
        print(f"  {BOLD}检测延迟总结{RESET}")
        print(f"  {'='*55}")
        print(f"  退化从 Phase 2 (R6) 开始: 回复变短、关键词覆盖下降、工具调用减少")
        print(f"  Gate7 (warning)  最早偏离信号: R{first_gate7_warn or '—'}")
        print(f"  Gate7 (critical) 严重偏离检测: R{first_gate7_crit or '—'}")
        print(f"  Gate3 (reject)   回归检测触发: R{first_gate3_rej or '—'}")
        print(f"  Gate6 (reject)   质量检测触发: R{first_gate6_rej or '—'}")
        print(f"  Gate6 是最强阻断门禁（LLM 判断），但存在随机性")
        print(f"  Gate7 在 observe 模式下可提前发现偏离趋势")
        print(f"  Gate3 在严重退化时（<基线 70%）才触发")
        print()

    finally:
        print(f"{BOLD}[清理] 恢复配置{RESET}")
        print("-" * 40)
        restore_config()
        if server_proc and server_proc.poll() is None:
            server_proc.kill()
            server_proc.wait(timeout=3)
        print()


if __name__ == "__main__":
    main()
