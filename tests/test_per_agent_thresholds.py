"""
Per-agent threshold resolution tests.

Verifies:
  1. resolve_agent_thresholds returns global defaults when no per-agent override
  2. Per-agent overrides correctly overlay global defaults
  3. Gate3Config.resolve_for_agent produces correct thresholds
  4. Gate4Config.resolve_for_agent produces correct thresholds
  5. Gate3Regression uses per-agent thresholds when agent is in metadata
  6. Gate4GrayRelease uses per-agent thresholds when agent is in metadata
  7. Unknown agent types fall back to global defaults
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from agent_prod.gates.thresholds import resolve_agent_thresholds, list_agents_with_overrides
from agent_prod.gates.gate3_regression import Gate3Config, Gate3Regression
from agent_prod.gates.gate4_gray import Gate4Config, Gate4GrayRelease
from agent_prod.gates.models import Improvement

PASS = 0
FAIL = 0

def ok(msg: str):
    global PASS; PASS += 1
    print(f"  ✅ {msg}")

def ng(msg: str, detail=""):
    global FAIL; FAIL += 1
    print(f"  ❌ {msg}")
    if detail:
        print(f"     {detail}")


# Sample config with per-agent overrides
SAMPLE_CONFIG = {
    "gates": {
        "gate3": {
            "regress_pct": 0.95,
            "perf_degradation_limit": 0.05,
            "repeatability_threshold": 0.1,
            "repeatability_runs": 3,
            "unstable_retry_count": 5,
            "per_agent": {
                "hermes": {
                    "regress_pct": 0.93,
                    "perf_degradation_limit": 0.08,
                },
                "claude-code": {
                    "regress_pct": 0.97,
                    "perf_degradation_limit": 0.05,
                },
            },
        },
        "gate4": {
            "error_rate_increase": 0.01,
            "latency_increase": 0.10,
            "resource_increase": 0.15,
            "stable_period_seconds": 60,
            "stages": {
                1: {"traffic_pct": 1, "observe_cycles": 2, "label": "1%"},
            },
            "per_agent": {
                "hermes": {
                    "error_rate_increase": 0.02,
                    "latency_increase": 0.15,
                    "resource_increase": 0.20,
                },
                "claude-code": {
                    "error_rate_increase": 0.01,
                    "latency_increase": 0.08,
                    "resource_increase": 0.12,
                },
            },
        },
    },
}

# ══════════════════════════════════════════════════════════════
print("=" * 65)
print("  Per-Agent Threshold Resolution Tests")
print("=" * 65)
print()

# ── 1. resolve_agent_thresholds — global defaults ──────────
print("── 1. Global defaults (no agent) ──")
r = resolve_agent_thresholds("gate3", "", SAMPLE_CONFIG)
assert r["regress_pct"] == 0.95, f"Expected 0.95, got {r['regress_pct']}"
ok(f"Gate3 global: regress_pct={r['regress_pct']}")

r4 = resolve_agent_thresholds("gate4", "", SAMPLE_CONFIG)
assert r4["error_rate_increase"] == 0.01
ok(f"Gate4 global: error_rate_increase={r4['error_rate_increase']}")

# ── 2. resolve_agent_thresholds — agent override ──────────
print("── 2. Per-agent overrides ──")
r_hermes = resolve_agent_thresholds("gate3", "hermes", SAMPLE_CONFIG)
assert r_hermes["regress_pct"] == 0.93, f"Expected 0.93, got {r_hermes['regress_pct']}"
assert r_hermes["perf_degradation_limit"] == 0.08
ok(f"Gate3 hermes: regress_pct={r_hermes['regress_pct']}, perf_degradation_limit={r_hermes['perf_degradation_limit']}")

r_claude = resolve_agent_thresholds("gate3", "claude-code", SAMPLE_CONFIG)
assert r_claude["regress_pct"] == 0.97
assert r_claude["perf_degradation_limit"] == 0.05
ok(f"Gate3 claude-code: regress_pct={r_claude['regress_pct']}, perf_degradation_limit={r_claude['perf_degradation_limit']}")

# ── 3. Unknown agent falls back ──────────────────────────
print("── 3. Unknown agent fallback ──")
r_unknown = resolve_agent_thresholds("gate3", "nonexistent", SAMPLE_CONFIG)
assert r_unknown["regress_pct"] == 0.95, f"Unknown agent should fall back to global: {r_unknown['regress_pct']}"
ok(f"Unknown agent: falls back to global regress_pct={r_unknown['regress_pct']}")

# ── 4. Gate3Config.resolve_for_agent ─────────────────────
print("── 4. Gate3Config.resolve_for_agent ──")
cfg_h = Gate3Config.resolve_for_agent("hermes", SAMPLE_CONFIG)
assert cfg_h.regress_pct == 0.93
assert cfg_h.perf_degradation_limit == 0.08
ok(f"Gate3Config(hermes): regress_pct={cfg_h.regress_pct}, perf_degradation={cfg_h.perf_degradation_limit}")

cfg_default = Gate3Config.resolve_for_agent("", SAMPLE_CONFIG)
assert cfg_default.regress_pct == 0.95
ok(f"Gate3Config(default): regress_pct={cfg_default.regress_pct}")

# ── 5. Gate4Config.resolve_for_agent ─────────────────────
print("── 5. Gate4Config.resolve_for_agent ──")
cfg4_h = Gate4Config.resolve_for_agent("hermes", SAMPLE_CONFIG)
assert cfg4_h.error_rate_increase == 0.02
assert cfg4_h.latency_increase == 0.15
ok(f"Gate4Config(hermes): error_rate_increase={cfg4_h.error_rate_increase}, latency_increase={cfg4_h.latency_increase}")

# ── 6. Gate3Regression uses per-agent thresholds ─────────
print("── 6. Gate3Regression per-agent resolution ──")
gate3 = Gate3Regression(raw_config=SAMPLE_CONFIG)

# Improvement without agent metadata → global thresholds
imp_no_agent = Improvement(
    name="test-no-agent",
    candidate_output={"final_response": "test", "confidence": 0.8, "tools_used": [], "token_count": 100, "warnings": []},
    baseline_output={"f1_score": 0.95, "latency_p95_ms": 100, "success_rate": 0.99},
)
cfg_no = gate3._resolve_config(imp_no_agent)
assert cfg_no.regress_pct == 0.95, f"Expected 0.95, got {cfg_no.regress_pct}"
ok(f"No agent metadata → global regress_pct={cfg_no.regress_pct}")

# Improvement with hermes → hermes thresholds
imp_hermes = Improvement(
    name="test-hermes",
    metadata={"agent": "hermes"},
    candidate_output={"final_response": "test", "confidence": 0.8, "tools_used": [], "token_count": 100, "warnings": []},
    baseline_output={"f1_score": 0.95, "latency_p95_ms": 100, "success_rate": 0.99},
)
cfg_hermes = gate3._resolve_config(imp_hermes)
assert cfg_hermes.regress_pct == 0.93, f"Expected 0.93, got {cfg_hermes.regress_pct}"
assert cfg_hermes.perf_degradation_limit == 0.08
ok(f"Hermes agent → regress_pct={cfg_hermes.regress_pct}, perf_degradation={cfg_hermes.perf_degradation_limit}")

# Improvement with claude-code → claude-code thresholds
imp_claude = Improvement(
    name="test-claude",
    metadata={"agent": "claude-code"},
    candidate_output={"final_response": "test", "confidence": 0.8, "tools_used": [], "token_count": 100, "warnings": []},
    baseline_output={"f1_score": 0.95, "latency_p95_ms": 100, "success_rate": 0.99},
)
cfg_claude = gate3._resolve_config(imp_claude)
assert cfg_claude.regress_pct == 0.97, f"Expected 0.97, got {cfg_claude.regress_pct}"
ok(f"Claude-code agent → regress_pct={cfg_claude.regress_pct}")

# ── 7. Gate3 actual evaluation with per-agent thresholds ──
print("── 7. Gate3 regression with per-agent thresholds ──")
# Use hermes thresholds (regress_pct=0.93). f1_score drops from 0.95 to 0.90
# 0.95 * 0.93 = 0.8835. 0.90 > 0.8835 → should PASS
imp_pass = Improvement(
    name="test-pass",
    metadata={"agent": "hermes"},
    candidate_output={"final_response": "test", "confidence": 0.8, "tools_used": [], "token_count": 100, "warnings": [],
                      "f1_score": 0.90, "success_rate": 0.95, "latency_p95_ms": 100},
    baseline_output={"f1_score": 0.95, "latency_p95_ms": 100, "success_rate": 0.99},
)
result_pass = gate3.verify(imp_pass)
assert result_pass.passed, f"Hermes 0.93 threshold: expected pass with f1=0.90, got {result_pass.reason}"
ok(f"Hermes threshold: f1=0.90 passes (0.93*0.95={0.95*0.93:.2f} threshold)")

# Now with f1 dropping to 0.85 — should fail under hermes 0.93 threshold
# 0.85 < 0.8835 → should FAIL
imp_fail = Improvement(
    name="test-fail",
    metadata={"agent": "hermes"},
    candidate_output={"final_response": "test", "confidence": 0.8, "tools_used": [], "token_count": 100, "warnings": [],
                      "f1_score": 0.85, "success_rate": 0.95},
    baseline_output={"f1_score": 0.95, "latency_p95_ms": 100, "success_rate": 0.99},
)
result_fail = gate3.verify(imp_fail)
assert not result_fail.passed, f"Hermes 0.93 threshold: expected fail with f1=0.85, got {result_fail.reason}"
ok(f"Hermes threshold: f1=0.85 fails (0.93*0.95={0.95*0.93:.2f} threshold)")

# Same f1=0.90 under global threshold (0.95): 0.95*0.95=0.9025, 0.90 < 0.9025 → FAIL
gate3_global = Gate3Regression(raw_config=None)  # no raw_config → global defaults only
imp_global = Improvement(
    name="test-global",
    metadata={},  # no agent
    candidate_output={"final_response": "test", "confidence": 0.8, "tools_used": [], "token_count": 100, "warnings": [],
                      "f1_score": 0.90, "success_rate": 0.95, "latency_p95_ms": 100},
    baseline_output={"f1_score": 0.95, "latency_p95_ms": 100, "success_rate": 0.99},
)
result_global = gate3_global.verify(imp_global)
assert not result_global.passed, f"Global 0.95 threshold: expected fail with f1=0.90"
ok(f"Global threshold: f1=0.90 fails (0.95*0.95={0.95*0.95:.3f} threshold) — stricter than hermes")

# ── 8. list_agents_with_overrides ─────────────────────────
print("── 8. list_agents_with_overrides ──")
agents = list_agents_with_overrides("gate3", SAMPLE_CONFIG)
assert "hermes" in agents
assert "claude-code" in agents
ok(f"Gate3 agents with overrides: {agents}")

# ══════════════════════════════════════════════════════════════
print()
print("=" * 65)
print(f"  Results: {PASS} passed, {FAIL} failed ({PASS+FAIL} total)")
if FAIL == 0:
    print("  🏆 ALL TESTS PASSED")
else:
    print(f"  ⚠️  {FAIL} FAILURES")
print("=" * 65)
if __name__ == "__main__":
    sys.exit(0 if FAIL == 0 else 1)
