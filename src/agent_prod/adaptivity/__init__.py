"""Adaptivity — self-improving systems (causal attribution, data flywheel, adaptive thresholds)."""

from .causal_attributor import (
    ols,
    adf_test,
    granger_causality,
    counterfactual_baseline,
    CausalAttributor,
    AttributionReport,
)

__all__ = [
    "ols", "adf_test", "granger_causality",
    "counterfactual_baseline", "CausalAttributor", "AttributionReport",
]
