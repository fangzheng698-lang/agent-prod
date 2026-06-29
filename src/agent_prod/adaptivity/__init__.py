"""Adaptivity — self-improving systems (causal attribution, data flywheel, adaptive thresholds, loop orchestrator)."""

from .causal_attributor import (
    AttributionReport,
    CausalAttributor,
    adf_test,
    counterfactual_baseline,
    granger_causality,
    ols,
)
from .loop_orchestrator import (
    CyclePhase,
    CycleResult,
    LoopOrchestrator,
)

__all__ = [
    "ols", "adf_test", "granger_causality",
    "counterfactual_baseline", "CausalAttributor", "AttributionReport",
    "LoopOrchestrator", "CyclePhase", "CycleResult",
]
