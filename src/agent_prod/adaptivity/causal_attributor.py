# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Phase 11: Causal Attribution — Granger Causality + Counterfactual.

Zero external dependencies (no statsmodels/scipy). All statistics from scratch:
  - OLS linear regression with R² and p-value
  - Augmented Dickey-Fuller (ADF) stationarity test
  - Granger causality F-test with auto-differencing
  - Counterfactual baseline projection via linear trend
  - CausalAttributor: full attribution pipeline over ExecutionLogRecords
"""

from __future__ import annotations

import math
from datetime import UTC
from typing import Any

from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════
# Statistical utilities
# ═══════════════════════════════════════════════════════════════════

def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _var(vals: list[float], ddof: int = 0) -> float:
    n = len(vals)
    if n <= ddof:
        return 0.0
    m = _mean(vals)
    return sum((x - m) ** 2 for x in vals) / (n - ddof)


def _std(vals: list[float], ddof: int = 1) -> float:
    return math.sqrt(_var(vals, ddof=ddof))


def _cov(x: list[float], y: list[float]) -> float:
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    mx, my = _mean(x), _mean(y)
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (n - 1)


# ═══════════════════════════════════════════════════════════════════
# 1. OLS — Ordinary Least Squares from scratch
# ═══════════════════════════════════════════════════════════════════

def ols(X: list[list[float]], y: list[float]) -> dict[str, Any]:
    """Ordinary Least Squares regression.

    X: design matrix (list of rows, each row = [1.0, x1, x2, ...])
    y: dependent variable

    Returns:
        dict with: coef (list), residuals, r_squared, p_value, f_stat, se (list)
    """
    n = len(X)
    if n == 0 or not X[0]:
        return {"coef": [], "residuals": [], "r_squared": 0.0,
                "p_value": 1.0, "f_stat": 0.0, "se": []}

    k = len(X[0])  # number of predictors (incl intercept)
    df_model = k - 1
    df_resid = n - k

    # X^T X  (k x k)
    XtX = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            XtX[i][j] = sum(X[r][i] * X[r][j] for r in range(n))

    # X^T y  (k x 1)
    Xty = [0.0] * k
    for i in range(k):
        Xty[i] = sum(X[r][i] * y[r] for r in range(n))

    # Invert X^T X via Gaussian elimination
    def _invert(mat: list[list[float]]) -> list[list[float]]:
        m = len(mat)
        aug = [row[:] + [1.0 if i == j else 0.0 for j in range(m)] for i, row in enumerate(mat)]
        for col in range(m):
            pivot = max(range(col, m), key=lambda r: abs(aug[r][col]))
            if abs(aug[pivot][col]) < 1e-14:
                return [[0.0] * m for _ in range(m)]
            aug[col], aug[pivot] = aug[pivot], aug[col]
            piv_val = aug[col][col]
            for c in range(2 * m):
                aug[col][c] /= piv_val
            for r in range(m):
                if r == col:
                    continue
                factor = aug[r][col]
                for c in range(col, 2 * m):
                    aug[r][c] -= factor * aug[col][c]
        return [row[m:] for row in aug]

    XtX_inv = _invert(XtX)
    coef = [sum(XtX_inv[i][j] * Xty[j] for j in range(k)) for i in range(k)]

    # Residuals
    y_hat = [sum(coef[j] * X[i][j] for j in range(k)) for i in range(n)]
    residuals = [y[i] - y_hat[i] for i in range(n)]

    # R²
    y_mean = _mean(y)
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    ss_res = sum(r ** 2 for r in residuals)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-14 else 0.0

    # Standard errors
    sigma2 = ss_res / df_resid if df_resid > 0 else 0.0
    se = [math.sqrt(sigma2 * XtX_inv[i][i]) if XtX_inv[i][i] > 0 else 0.0 for i in range(k)]

    # F-statistic and p-value (overall regression)
    if df_model > 0:
        ss_reg = ss_tot - ss_res
        ms_reg = ss_reg / df_model
        ms_res = sigma2
        f_stat = ms_reg / ms_res if ms_res > 0 else 0.0
        p_value = _f_survival(f_stat, df_model, df_resid)
    else:
        f_stat = 0.0
        p_value = 1.0

    return {
        "coef": coef,
        "residuals": residuals,
        "r_squared": max(0.0, min(1.0, r_squared)),
        "p_value": p_value,
        "f_stat": f_stat,
        "se": se,
    }


# ═══════════════════════════════════════════════════════════════════
# F-distribution survival function (via incomplete beta)
# ═══════════════════════════════════════════════════════════════════

def _lgamma(x: float) -> float:
    """Log-gamma via Stirling approximation."""
    if x <= 0:
        return 0.0
    if x < 10:
        s = 1.0
        result = -x
        for k in range(1, 20):
            s *= x / k
            if s < 1e-14:
                break
            result += s / k
        return result + x * math.log(x) - x + 0.5 * math.log(2 * math.pi / x)
    return (x - 0.5) * math.log(x) - x + 0.5 * math.log(2 * math.pi)


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function (continued fraction)."""
    if x < 0.0 or x > 1.0:
        return 0.0
    if x == 0.0 or x == 1.0:
        return x

    # Use the smaller of x and 1-x for convergence
    swap = x > (a + 1.0) / (a + b + 2.0)
    if swap:
        x = 1.0 - x
        a, b = b, a

    # Compute log(B(a,b))
    log_beta = _lgamma(a) + _lgamma(b) - _lgamma(a + b)

    # Continued fraction (Lentz's method)
    _fpmax = 1e300
    tiny = 1e-30
    c = 1.0
    d = 1.0 - (a + b) * x / (a + 1.0)
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        mm2 = 2 * m
        aa = m * (b - m) * x / ((a + mm2 - 1) * (a + mm2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (a + b + m) * x / ((a + mm2) * (a + mm2 + 1))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        del_ = d * c
        h *= del_
        if abs(del_ - 1.0) < 3e-7:
            break

    result = math.exp(a * math.log(x) + b * math.log(1.0 - x) - log_beta) * h / a
    if swap:
        return 1.0 - result
    return result


def _f_survival(f: float, df1: int, df2: int) -> float:
    """F-distribution survival function (p-value from F-statistic)."""
    if f <= 0:
        return 1.0
    if df1 <= 0 or df2 <= 0:
        return 1.0
    x = df2 / (df2 + df1 * f)
    return _betai(df2 / 2.0, df1 / 2.0, x)


def _t_survival_two_sided(t: float, df: float) -> float:
    """Two-sided p-value from t-statistic."""
    if df <= 0:
        return 1.0
    x = df / (df + t * t)
    return 2.0 * _betai(df / 2.0, 0.5, x)


# ═══════════════════════════════════════════════════════════════════
# 2. ADF — Augmented Dickey-Fuller stationarity test
# ═══════════════════════════════════════════════════════════════════

def adf_test(series: list[float], max_lag: int = 5,
             significance: float = 0.05) -> dict[str, Any]:
    """Augmented Dickey-Fuller test for stationarity.

    H0: series has a unit root (non-stationary).
    H1: series is stationary.

    Uses OLS with constant term. Lag selection via AIC-like rule.

    Returns:
        dict with: stationary (bool), p_value, statistic, critical_value, lag
    """
    n = len(series)
    if n < 10:
        return {"stationary": False, "p_value": 1.0, "statistic": 0.0,
                "critical_value": 0.0, "lag": 0}

    # Select optimal lag via t-statistic significance
    best_lag = 0
    best_t = -1e9
    for lag in range(min(max_lag + 1, n // 3)):
        dy = [series[i] - series[i - 1] for i in range(1, n)]
        m = len(dy) - lag
        if m < 5 + lag:
            continue
        X_rows = []
        y_rows = []
        for t in range(lag, len(dy)):
            row = [1.0, series[t]]  # intercept + lagged level
            for p in range(1, lag + 1):
                row.append(dy[t - p])  # lagged differences
            X_rows.append(row)
            y_rows.append(dy[t])
        result = ols(X_rows, y_rows)
        if result["se"] and result["se"][1] > 0:
            t_stat = result["coef"][1] / result["se"][1]
            if abs(t_stat) > abs(best_t):
                best_t = t_stat
                best_lag = lag

    # Run ADF with best lag
    dy = [series[i] - series[i - 1] for i in range(1, n)]
    m = len(dy) - best_lag
    if m < 5 + best_lag:
        return {"stationary": False, "p_value": 1.0, "statistic": 0.0,
                "critical_value": 0.0, "lag": best_lag}

    X_rows, y_rows = [], []
    for t in range(best_lag, len(dy)):
        row = [1.0, series[t]]
        for p in range(1, best_lag + 1):
            row.append(dy[t - p])
        X_rows.append(row)
        y_rows.append(dy[t])

    result = ols(X_rows, y_rows)
    if not result["se"] or result["se"][1] <= 0:
        t_stat = 0.0
        p_value = 1.0
    else:
        t_stat = result["coef"][1] / result["se"][1]
        # Use t-distribution survival for p-value (ADF uses non-standard critical values,
        # but t-distribution is a reasonable approximation for n>50)
        p_value = _t_survival_two_sided(t_stat, m - len(X_rows[0]))

    # Critical value approximation for ADF at 5% (from Hamilton, depends on sample size)
    # For n ~ 100, critical value ~ -2.89 (intercept only)
    if n >= 100:
        critical = -2.89
    elif n >= 50:
        critical = -2.93
    else:
        critical = -2.95

    return {
        "stationary": t_stat < critical and p_value < 0.05,
        "statistic": t_stat,
        "p_value": p_value,
        "critical_value": critical,
        "lag": best_lag,
    }


# ═══════════════════════════════════════════════════════════════════
# 3. Granger Causality
# ═══════════════════════════════════════════════════════════════════

def _difference(series: list[float]) -> list[float]:
    """First difference: x[t] - x[t-1]."""
    return [series[i] - series[i - 1] for i in range(1, len(series))]


def granger_causality(x: list[float], y: list[float], max_lag: int = 5,
                      significance: float = 0.05,
                      auto_diff: bool = False) -> dict[str, Any]:
    """Granger causality test: does X help predict Y?

    H0: X does NOT Granger-cause Y.

    If auto_diff=True, differencing is applied when ADF indicates non-stationarity.

    Returns:
        dict with: causal (bool), p_value, best_lag, f_stat, lag_results
    """
    n = len(x)
    if n < 10:
        return {"causal": False, "p_value": 1.0, "best_lag": 0,
                "f_stat": 0.0, "lag_results": []}

    x_use, y_use = x, y

    # Auto-differencing if data is non-stationary
    if auto_diff:
        adf_x = adf_test(x, max_lag=min(5, n // 3))
        adf_y = adf_test(y, max_lag=min(5, n // 3))
        if not adf_x["stationary"]:
            x_use = _difference(x)
            n_x = len(x_use)
            if n_x < 10:
                return {"causal": False, "p_value": 1.0, "best_lag": 0,
                        "f_stat": 0.0, "lag_results": []}
        if not adf_y["stationary"]:
            y_use = _difference(y)
            n_y = len(y_use)
            if n_y < 10:
                return {"causal": False, "p_value": 1.0, "best_lag": 0,
                        "f_stat": 0.0, "lag_results": []}
        # Align lengths
        min_len = min(len(x_use), len(y_use))
        x_use = x_use[:min_len]
        y_use = y_use[:min_len]

    n_use = len(x_use)
    effective_max_lag = min(max_lag, n_use // 5)

    best_result = None
    lag_results = []

    for lag in range(1, effective_max_lag + 1):
        m = n_use - lag
        if m < lag + 2:
            continue

        # Restricted model: Y regressed on lagged Y only
        X_restricted = [[1.0] + [y_use[t - p - 1] for p in range(lag)]
                        for t in range(lag, n_use)]
        y_vec = y_use[lag:]

        res_restricted = ols(X_restricted, y_vec)
        ssr_r = sum(r ** 2 for r in res_restricted["residuals"])

        # Unrestricted model: Y regressed on lagged Y AND lagged X
        X_unrestricted = []
        for t in range(lag, n_use):
            row = [1.0]
            row.extend(y_use[t - p - 1] for p in range(lag))
            row.extend(x_use[t - p - 1] for p in range(lag))
            X_unrestricted.append(row)

        res_unrestricted = ols(X_unrestricted, y_vec)
        ssr_u = sum(r ** 2 for r in res_unrestricted["residuals"])

        # F statistic
        df_resid_u = len(y_vec) - len(X_unrestricted[0])  # n - k_unrestricted
        if df_resid_u <= 0:
            continue

        # Number of parameter restrictions = lag (the X lag terms)
        f_num = (ssr_r - ssr_u) / lag if lag > 0 else 0.0
        f_den = ssr_u / df_resid_u if df_resid_u > 0 and ssr_u > 1e-14 else 1.0
        f_stat = f_num / f_den if f_den > 0 else 0.0

        p_value = _f_survival(max(f_stat, 0.0), lag, df_resid_u)
        # Bonferroni correction for multiple lag testing
        p_value_bonf = min(1.0, p_value * effective_max_lag)

        lag_results.append({
            "lag": lag,
            "f_stat": f_stat,
            "p_value": p_value,
            "causal": p_value_bonf < significance,
        })

        if best_result is None or p_value < best_result.get("p_value", 1.0):
            best_result = lag_results[-1]

    if best_result is None:
        return {"causal": False, "p_value": 1.0, "best_lag": 0,
                "f_stat": 0.0, "lag_results": []}

    return {
        "causal": best_result["causal"],
        "p_value": best_result["p_value"],
        "best_lag": best_result["lag"],
        "f_stat": best_result["f_stat"],
        "lag_results": lag_results,
    }


# ═══════════════════════════════════════════════════════════════════
# 4. Counterfactual Baseline
# ═══════════════════════════════════════════════════════════════════

def counterfactual_baseline(
    pre_period: list[float],
    post_observed: list[float],
    post_timestamps: list[float],
    significance: float = 0.05,
) -> dict[str, Any]:
    """Counterfactual baseline: projects pre-period trend to post-period.

    Uses OLS on pre_period to estimate trend, then projects into post_period.
    Deviation is measured as mean percent difference from counterfactual.

    Returns:
        dict with: deviation_detected (bool), mean_deviation_pct, deviation_significant,
                   counterfactual (list), residuals, r_squared, coefficient
    """
    n_pre = len(pre_period)
    n_post = len(post_observed)

    if n_pre < 3:
        return {
            "deviation_detected": False,
            "mean_deviation_pct": 0.0,
            "deviation_significant": False,
            "counterfactual": [],
            "residuals": [],
            "r_squared": 0.0,
            "coefficient": 0.0,
        }

    # Fit OLS on pre-period: value = a + b * t
    X_pre = [[1.0, float(i)] for i in range(n_pre)]
    result = ols(X_pre, pre_period)
    intercept, slope = result["coef"][0], result["coef"][1]

    # Project counterfactual for post-period
    counterfactual = [intercept + slope * (n_pre + i) for i in range(n_post)]

    # Compute deviations
    deviations = [
        ((post_observed[i] - counterfactual[i]) / counterfactual[i] * 100)
        if counterfactual[i] > 1e-14 else 0.0
        for i in range(n_post)
    ]
    mean_deviation = sum(deviations) / n_post if n_post > 0 else 0.0

    # Test if deviation is significant via one-sample t-test on deviations
    if n_post >= 2:
        dev_mean = _mean(deviations)
        dev_std = _std(deviations)
        if dev_std > 0:
            t_stat = dev_mean / (dev_std / math.sqrt(n_post))
            p_value = _t_survival_two_sided(t_stat, n_post - 1)
        else:
            p_value = 1.0
    else:
        p_value = 1.0

    deviation_significant = p_value < significance

    return {
        "deviation_detected": abs(mean_deviation) > 5.0,  # 5% threshold
        "mean_deviation_pct": abs(mean_deviation),
        "deviation_significant": deviation_significant,
        "counterfactual": counterfactual,
        "residuals": result["residuals"],
        "r_squared": result["r_squared"],
        "coefficient": slope,
    }


# ═══════════════════════════════════════════════════════════════════
# 5. CausalAttributor — Full attribution pipeline
# ═══════════════════════════════════════════════════════════════════

class AttributionReport(BaseModel):
    """Full causal attribution report."""
    attributions: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def _get_log_attr(log: Any, attr_name: str) -> float | None:
    """Extract a numeric attribute from an ExecutionLogRecord, checking
    field, costs dict, and gate_passed.
    """
    if attr_name == "gate_passed":
        val = getattr(log, attr_name, None)
        return 1.0 if val else 0.0

    # Try direct attribute
    val = getattr(log, attr_name, None)
    if isinstance(val, (int, float)):
        return float(val)

    # Try costs dict
    costs = getattr(log, "costs", {})
    if isinstance(costs, dict) and attr_name in costs:
        cval = costs[attr_name]
        if isinstance(cval, (int, float)):
            return float(cval)

    # Try to convert
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class CausalAttributor:
    """Causal attribution engine.

    For each hypothesis (pre/post execution log sets), runs:
      1. Granger causality on each candidate variable
      2. Counterfactual baseline projection
      3. Verdict synthesis
    """

    def __init__(self, min_pre_samples: int = 20, significance: float = 0.05):
        self.min_pre_samples = min_pre_samples
        self.significance = significance

    def attribute(self, hypotheses: list[dict[str, Any]]) -> AttributionReport:
        """Run full attribution pipeline.

        Each hypothesis dict:
            name: str
            pre_logs: list[ExecutionLogRecord]
            post_logs: list[ExecutionLogRecord]
            candidate_vars: list[str]  (e.g. ["duration_ms", "tokens_used"])

        Returns:
            AttributionReport with attributions list and summary.
        """
        attributions = []

        for hyp in hypotheses:
            name = hyp["name"]
            pre_logs = hyp.get("pre_logs", [])
            post_logs = hyp.get("post_logs", [])
            candidate_vars = hyp.get("candidate_vars", [])

            if len(pre_logs) < self.min_pre_samples:
                attributions.append({
                    "hypothesis": name,
                    "verdict": "INSUFFICIENT_DATA",
                    "confidence": 0.0,
                    "causal_for": [],
                    "causal_details": [],
                    "counterfactual": {},
                })
                continue

            causal_for = []
            causal_details = []

            # Sort logs by timestamp for time-series ordering
            pre_logs_sorted = sorted(pre_logs,
                                     key=lambda r: getattr(r, "created_at",
                                                           getattr(r, "timestamp", "0")))

            for var in candidate_vars:
                # Build time series from pre logs
                pre_series = []
                for log in pre_logs_sorted:
                    val = _get_log_attr(log, var)
                    if val is not None:
                        pre_series.append(val)

                # Build time series from post logs
                post_logs_sorted = sorted(post_logs,
                                          key=lambda r: getattr(r, "created_at",
                                                               getattr(r, "timestamp", "0")))
                post_series = []
                for log in post_logs_sorted:
                    val = _get_log_attr(log, var)
                    if val is not None:
                        post_series.append(val)

                if len(pre_series) < 10 or len(post_series) < 3:
                    continue

                # Concatenate pre + post for Granger
                combined = pre_series + post_series
                # Granger: do past values of the variable "cause" spikes?
                # We test lagged self-causality: can lagged values predict current?
                # Use a synthetic indicator: 0 for pre, 1 for post
                n_total = len(combined)
                indicator = [0.0] * len(pre_series) + [1.0] * len(post_series)
                # Trim to equal lengths
                min_len = min(n_total, len(indicator))
                combined = combined[:min_len]
                indicator = indicator[:min_len]

                if len(combined) < 10:
                    continue

                # Granger: does indicator (pre/post) Granger-cause the variable?
                g = granger_causality(indicator, combined, max_lag=3,
                                      significance=self.significance,
                                      auto_diff=False)

                if g["causal"]:
                    causal_for.append(var)
                    causal_details.append({
                        "variable": var,
                        "granger": g,
                    })

            # Counterfactual: use mean of candidate vars from pre as baseline
            # Compute per-log aggregate score
            pre_scores = []
            for log in pre_logs_sorted:
                vals = []
                for var in candidate_vars:
                    v = _get_log_attr(log, var)
                    if v is not None:
                        vals.append(v)
                if vals:
                    pre_scores.append(sum(vals) / len(vals))

            post_scores = []
            for log in post_logs_sorted:
                vals = []
                for var in candidate_vars:
                    v = _get_log_attr(log, var)
                    if v is not None:
                        vals.append(v)
                if vals:
                    post_scores.append(sum(vals) / len(vals))

            cf_result: dict[str, Any] = {"deviation_detected": False,
                                          "mean_deviation_pct": 0.0,
                                          "deviation_significant": False}
            if pre_scores and post_scores:
                cf_result = counterfactual_baseline(
                    pre_period=pre_scores,
                    post_observed=post_scores,
                    post_timestamps=list(range(len(pre_scores),
                                               len(pre_scores) + len(post_scores))),
                    significance=self.significance,
                )

            # Verdict synthesis
            if len(causal_for) >= 1 and cf_result.get("deviation_detected"):
                verdict = "CAUSAL_LINK_DETECTED"
                confidence = 0.85
            elif len(causal_for) >= 1:
                verdict = "CAUSAL_SIGNAL_PRESENT"
                confidence = 0.60
            elif cf_result.get("deviation_detected"):
                verdict = "DEVIATION_ONLY"
                confidence = 0.40
            else:
                verdict = "NO_EVIDENCE"
                confidence = 0.15

            attributions.append({
                "hypothesis": name,
                "verdict": verdict,
                "confidence": confidence,
                "causal_for": causal_for,
                "causal_details": causal_details,
                "counterfactual": cf_result,
            })

        # Summary
        detected = sum(1 for a in attributions
                       if a["verdict"] in ("CAUSAL_LINK_DETECTED", "CAUSAL_SIGNAL_PRESENT"))
        if not attributions:
            summary = "No hypotheses tested."
        elif detected == 0:
            summary = "No causal relationships detected."
        elif detected == len(attributions):
            summary = (f"Causal attribution complete: {detected}/{len(attributions)} "
                       f"hypotheses show causal signals.")
        else:
            summary = (f"Causal attribution complete: {detected}/{len(attributions)} "
                       f"hypotheses show causal signals.")
        from datetime import datetime
        return AttributionReport(
            attributions=attributions,
            summary=summary,
            created_at=datetime.now(UTC).isoformat(),
        )
