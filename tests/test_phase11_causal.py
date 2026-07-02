"""Phase 11: Causal Attribution — Granger Causality + Counterfactual 测试

不依赖 statsmodels/scipy。所有统计从 scratch: OLS, Granger F-test, ADF stationarity。
"""
import sys, os, random, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0; FAIL = 0
random.seed(42)
def ok(msg): global PASS; PASS += 1; print(f"  ✅ {msg}")
def ng(msg, det=""):
    global FAIL; FAIL += 1; print(f"  ❌ {msg}")
    if det: print(f"     {det}")

print("=" * 60)
print("  Phase 11: Causal Attribution (Granger + Counterfactual)")
print("=" * 60)

from agent_prod.adaptivity.causal_attributor import (
    ols, granger_causality, counterfactual_baseline,
    CausalAttributor, AttributionReport, adf_test,
)

# ── 1. OLS from scratch ──
print("\n── 1. OLS — Simple linear regression ──")
X_data = [[1, 1], [1, 2], [1, 3], [1, 4], [1, 5]]
y_data = [2.1, 4.0, 5.9, 8.1, 10.0]
result = ols(X_data, y_data)
assert abs(result["coef"][0] - 0.0) < 0.5    # intercept ~0
assert abs(result["coef"][1] - 2.0) < 0.1    # slope ~2
assert result["r_squared"] > 0.99
assert result["p_value"] < 0.001
ok(f"OLS: slope≈{result['coef'][1]:.2f}, R²={result['r_squared']:.4f}")

# ── 2. Granger causality — X causes Y ──
print("\n── 2. Granger — Clear causal signal ──")
n = 100
# Y depends on lagged X (causal)
X_vals = [random.gauss(0, 1) for _ in range(n + 3)]
Y_vals = []
for t in range(n + 3):
    if t == 0:
        Y_vals.append(random.gauss(0, 1))
    else:
        Y_vals.append(0.5 * Y_vals[t-1] + 0.4 * X_vals[t-2] + random.gauss(0, 0.5))

g = granger_causality(X_vals[-n:], Y_vals[-n:], max_lag=3)
assert g["causal"] == True, f"Expected causal=True, got: {g}"
assert g["best_lag"] >= 1
assert g["p_value"] < 0.01
ok(f"Granger: causal={g['causal']}, p={g['p_value']:.6f}, lag={g['best_lag']}")

# ── 3. Granger — No causality (independent series) ──
print("\n── 3. Granger — No causality ──")
X_ind = [random.gauss(0, 1) for _ in range(100)]
Y_ind = [random.gauss(5, 2) for _ in range(100)]
g2 = granger_causality(X_ind, Y_ind, max_lag=3)
assert g2["causal"] == False
ok(f"No causality detected: p={g2['p_value']:.4f} (expected > 0.05)")

# ── 4. Granger — Bidirectional (test X→Y and Y→X) ──
print("\n── 4. Granger — Bidirectional test ──")
# X causes Y but not vice versa
X_bidir = [random.gauss(0, 1) for _ in range(n + 3)]
Y_bidir = []
for t in range(n + 3):
    if t == 0:
        Y_bidir.append(random.gauss(0, 1))
    else:
        Y_bidir.append(0.3 * Y_bidir[t-1] + 0.7 * X_bidir[t-1] + random.gauss(0, 0.2))

g_x2y = granger_causality(X_bidir[-n:], Y_bidir[-n:], max_lag=3)
g_y2x = granger_causality(Y_bidir[-n:], X_bidir[-n:], max_lag=3)
assert g_x2y["causal"] == True, f"X→Y should be causal: {g_x2y}"
assert g_y2x["causal"] == False, f"Y→X should NOT be causal: {g_y2x}"
ok("Directionality correct: X→Y causal, Y→X not")

# ── 5. Counterfactual baseline ──
print("\n── 5. Counterfactual — Linear trend projection ──")
pre_window = list(range(1, 21))  # 1..20
post_actual = [30, 33, 36, 39, 42]  # steep increase after intervention
cf = counterfactual_baseline(
    pre_period=pre_window,
    post_observed=post_actual,
    post_timestamps=list(range(21, 26)),
)
assert cf["deviation_detected"] == True
assert cf["mean_deviation_pct"] > 30  # >30% above counterfactual
assert cf["deviation_significant"] == True
assert "counterfactual" in cf
ok(f"Deviation: {cf['mean_deviation_pct']:.1f}% above counterfactual, significant={cf['deviation_significant']}")

# ── 6. Counterfactual — No deviation ──
print("\n── 6. Counterfactual — No deviation ──")
pre_stable = list(range(1, 21))
post_stable = [21, 22, 23, 24, 25]  # on-trend
cf2 = counterfactual_baseline(pre_stable, post_stable, list(range(21, 26)))
assert cf2["deviation_detected"] == False
ok(f"No deviation: {cf2['mean_deviation_pct']:.1f}% off trend")

# ── 7. ADF stationarity test ──
print("\n── 7. ADF Test — Stationary vs Trending ──")
stationary = [random.gauss(0, 1) for _ in range(100)]
trending = [i * 0.1 + random.gauss(0, 0.5) for i in range(100)]

a1 = adf_test(stationary)
a2 = adf_test(trending)
assert a1["stationary"] == True, f"Stationary series should pass ADF: {a1}"
assert a2["stationary"] == False, f"Trending series should fail ADF: {a2}"
ok(f"ADF: stationary={a1['stationary']} (p≈{a1['p_value']:.3f}), trending={a2['stationary']} (p≈{a2['p_value']:.3f})")

# ── 8. CausalAttributor — Full attribution pipeline ──
print("\n── 8. CausalAttributor — Full pipeline ──")
from agent_prod.observability.execution_log import ExecutionLogRecord
from datetime import UTC, datetime, timedelta

now = datetime.now(UTC)
# Pre-change: stable performance (v3)
pre_logs = []
for i in range(30):
    pre_logs.append(ExecutionLogRecord(
        run_id=f"pre_{i}", session_id="attr_test",
        timestamp=(now - timedelta(hours=30 - i)).isoformat(),
        duration_ms=2000 + random.gauss(0, 100),
        tokens_used=400 + random.gauss(0, 20),
        turns=1, gate_passed=True, prompt="test", response="ok",
    ))

# Post-change: significantly worse (v4 upgrade)
post_logs = []
for i in range(10):
    post_logs.append(ExecutionLogRecord(
        run_id=f"post_{i}", session_id="attr_test",
        timestamp=(now - timedelta(hours=10 - i)).isoformat(),
        duration_ms=4000 + random.gauss(0, 200),  # 2x slower!
        tokens_used=550 + random.gauss(0, 30),    # more tokens
        turns=1, gate_passed=True, prompt="test", response="ok",
    ))

attributor = CausalAttributor(min_pre_samples=20)
hypotheses = [
    {"name": "model_upgrade", "pre_logs": pre_logs, "post_logs": post_logs,
     "candidate_vars": ["duration_ms", "tokens_used"]},
]
report = attributor.attribute(hypotheses)
assert report is not None
assert report.attributions is not None
assert len(report.attributions) >= 1

# Find model_upgrade attribution
model_attr = None
for a in report.attributions:
    if a["hypothesis"] == "model_upgrade":
        model_attr = a
        break
assert model_attr is not None
assert model_attr["causal_for"] is not None
assert len(model_attr["causal_for"]) >= 1
assert model_attr["counterfactual"]["deviation_detected"] == True
ok(f"Attribution: {model_attr['verdict']} (confidence: {model_attr['confidence']:.2f})")
ok(f"  Causal for: {model_attr['causal_for']}")
ok(f"  Counterfactual: {model_attr['counterfactual']['mean_deviation_pct']:.1f}% deviation")

# ── 9. AttributionReport serialization ──
print("\n── 9. AttributionReport — Serialization ──")
d = report.to_dict()
assert "attributions" in d
assert "summary" in d
assert len(d["attributions"]) >= 1
ok(f"Report serializable: {len(d['attributions'])} attributions, summary='{d['summary'][:80]}...'")

# ── 10. Granger with differencing for non-stationary data ──
print("\n── 10. Granger on differenced data ──")
trend_x = [i * 0.3 + random.gauss(0, 0.2) for i in range(100)]
trend_y = []
for t in range(100):
    if t == 0:
        trend_y.append(random.gauss(0, 0.2))
    else:
        trend_y.append(trend_y[t-1] * 0.4 + trend_x[t-1] * 0.6 + random.gauss(0, 0.2))

g_diff = granger_causality(trend_x, trend_y, max_lag=3, auto_diff=True)
assert g_diff["causal"] == True, f"Should detect causality after differencing: {g_diff}"
ok(f"Differenced Granger: causal={g_diff['causal']}, p={g_diff['p_value']:.6f}")

print()
print("=" * 60)
print(f"  Phase 11: {PASS} passed, {FAIL} failed ({PASS+FAIL} total)")
if FAIL == 0: print("  ✅ ALL TESTS PASSED")
else: print(f"  ❌ {FAIL} FAILURES")
print("=" * 60)
if __name__ == "__main__":
    sys.exit(0 if FAIL == 0 else 1)
