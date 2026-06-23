"""
Quality Gate Plugin Interface — the standard contract for all gates.

Any gate (built-in or third-party) MUST implement this ABC.
The engine discovers and runs gates through this interface,
not through hardcoded knowledge of concrete gate classes.

This is the "interface standard" that makes the quality gates system
extensible: add a new gate by subclassing GatePlugin and registering it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ARCHITECTURAL GUARANTEE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The QualityGateEngine interacts with gates exclusively through this ABC.
It never imports or references concrete gate classes directly.
This means:
  - Gate order is configurable, not hardcoded
  - Third-party gates work without engine modification
  - Removing/replacing a gate requires zero engine changes
  - Gate authors only need to know this interface
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import GateResult, GateName, Improvement, RollbackLevel


class GatePlugin(ABC):
    """Abstract base class for all quality gates.

    Each gate is a plug-in that:
      1. Verifies an Improvement against its criteria
      2. Can rollback if verification fails
      3. Is instantiated from a configuration dictionary

    Minimal implementation (only three methods required):

        class Gate6Safety(GatePlugin):
            name = GateName("gate6_safety")

            def verify(self, improvement: Improvement) -> GateResult:
                ...

            def rollback(self, improvement: Improvement) -> None:
                ...

            @classmethod
            def from_config(cls, config: dict, name: GateName) -> "Gate6Safety":
                return cls(name)
    """

    # ── Gate identity ──────────────────────────────────────

    name: GateName
    """Unique identifier, used for result tracking and logging."""

    rollback_level: RollbackLevel = RollbackLevel.L1
    """Severity of rollback needed if this gate fails."""

    # ── Public API ─────────────────────────────────────────

    @abstractmethod
    def verify(self, improvement: Improvement) -> GateResult:
        """Run this gate's checks against the improvement.

        Called by QualityGateEngine.run_pipeline() during the sequential
        gate evaluation loop.

        Args:
            improvement: The improvement being evaluated. Contains
                candidate/baseline outputs, trace data, budget info,
                and results from earlier gates (for cross-gate logic).

        Returns:
            GateResult with passed=True if the improvement meets this
            gate's criteria, passed=False otherwise.

        Important:
            - Mutations to 'improvement' are allowed (e.g., to attach
              metadata or warnings to the improvement for later gates).
            - This method should be STATELESS — repeated calls with the
              same improvement should produce the same result.
            - The engine wraps this call with timeout protection, so
              the implementation does not need its own timeout logic.
        """
        ...

    @abstractmethod
    def rollback(self, improvement: Improvement) -> None:
        """Execute rollback actions when this gate fails.

        Called by the engine immediately after verify() returns
        passed=False. The gate is responsible for cleaning up any
        side effects. Rollback failures are caught and logged by
        the engine — they never propagate to the caller.

        Args:
            improvement: The improvement that failed verification.
        """
        ...

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict, name: GateName) -> "GatePlugin":
        """Factory: create a gate instance from configuration.

        Args:
            config: The full quality_gates configuration dictionary
                    (from config.yaml or equivalent).
            name:   The GateName to assign to this instance.

        Returns:
            A configured GatePlugin instance ready for use.

        Example:
            >>> gate = MyGate.from_config(
            ...     config={"my_gate": {"threshold": 0.95}},
            ...     name=GateName("gate6_safety"),
            ... )
        """
        ...

    # ── Metadata (optional override) ────────────────────────

    @property
    def description(self) -> str:
        """Human-readable description of what this gate validates."""
        return self.__class__.__doc__ or f"Gate: {self.name.value}"

    @property
    def version(self) -> str:
        """Semantic version of this gate implementation."""
        return "1.0.0"


# ═════════════════════════════════════════════════════════════════════
# Gate Registration — discoverability without hardcoding
# ═════════════════════════════════════════════════════════════════════

# Global registry of gate factory functions.
# Populated by each gate module at import time via register_gate().
# The engine reads this registry to build the pipeline.

_GATE_REGISTRY: dict[GateName, type[GatePlugin]] = {}


def register_gate(name: GateName, plugin_cls: type[GatePlugin]) -> None:
    """Register a gate plugin class for a given gate name.

    Called at module import time by concrete gate implementations:

        from .interface import register_gate
        register_gate(GateName.GATE1, Gate1Execution)
    """
    _GATE_REGISTRY[name] = plugin_cls


def get_gate(name: GateName) -> type[GatePlugin] | None:
    """Look up a registered gate plugin class by name."""
    return _GATE_REGISTRY.get(name)


def list_registered_gates() -> list[GateName]:
    """Return all registered gate names in no particular order."""
    return list(_GATE_REGISTRY.keys())


def get_registered_gate_classes() -> dict[GateName, type[GatePlugin]]:
    """Return a copy of the full registry dict."""
    return dict(_GATE_REGISTRY)
