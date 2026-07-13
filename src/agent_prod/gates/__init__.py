"""
Quality Gates — plug-in architecture for agent output validation.

Standard interface: GatePlugin ABC (see interface.py)
  1. Gate1: Execution validation (structured output contract)
  2. Gate2: Trace integrity (LLM ↔ tool correspondence)
  3. Gate3: Regression detection (output quality monitoring)
  4. Gate4: Gray release (gradual traffic ramp)
  5. Gate5: Release audit (policy-as-code)

To add a new gate:
  1. Subclass GatePlugin
  2. Call register_gate(name, YourClass) at module load
  3. Add it to the pipeline order in config
"""

from .engine import QualityGateEngine
from .interface import (
    GatePlugin,
    get_gate,
    get_registered_gate_classes,
    list_registered_gates,
    register_gate,
)
from .models import (
    GateName,
    GateResult,
    Improvement,
    ImprovementStatus,
    RollbackLevel,
    RollbackPlan,
)

__all__ = [
    "GatePlugin",
    "register_gate",
    "get_gate",
    "list_registered_gates",
    "get_registered_gate_classes",
    "GateResult",
    "GateName",
    "Improvement",
    "ImprovementStatus",
    "RollbackLevel",
    "RollbackPlan",
    "QualityGateEngine",
]
