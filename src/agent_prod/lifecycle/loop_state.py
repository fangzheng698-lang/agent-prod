"""11-state state machine for closed-loop orchestrator cycle.

States: CANDIDATE → EXECUTING → EXECUTED → ATTRIBUTING → ATTRIBUTED
        → OPTIMIZING → OPTIMIZED → VERIFYING → VERIFIED
        → RELEASING → COMPLETED

Rejected states (terminal): REJECTED, ROLLED_BACK, ERROR

Usage:
    sm = LoopStateMachine("my-cycle")
    sm.transition(LoopState.EXECUTING)
    print(sm.current)  # LoopState.EXECUTING
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class LoopState(str, Enum):
    """11 production states + 3 terminal states for a loop cycle."""
    # ── Production states (11) ──
    CANDIDATE = "candidate"           # cycle created
    EXECUTING = "executing"           # agent running
    EXECUTED = "executed"             # gates evaluated
    ATTRIBUTING = "attributing"       # causal analysis running
    ATTRIBUTED = "attributed"         # attribution complete
    OPTIMIZING = "optimizing"         # flywheel + optimizer running
    OPTIMIZED = "optimized"           # reports generated
    VERIFYING = "verifying"           # replay + benchmark checks
    VERIFIED = "verified"             # all pre-release checks passed
    RELEASING = "releasing"           # promoting / rolling back
    COMPLETED = "completed"           # cycle finished

    # ── Terminal states ──
    REJECTED = "rejected"             # gates failed, no recovery
    ROLLED_BACK = "rolled_back"       # release rolled back
    ERROR = "error"                   # unexpected error

    # ── Query helpers ──
    @property
    def is_terminal(self) -> bool:
        return self in (LoopState.REJECTED, LoopState.ROLLED_BACK, LoopState.ERROR, LoopState.COMPLETED)

    @property
    def is_active(self) -> bool:
        return not self.is_terminal

    @property
    def phase(self) -> str:
        """Which phase of the loop does this state belong to?"""
        _map = {
            LoopState.CANDIDATE: "init",
            LoopState.EXECUTING: "execution",
            LoopState.EXECUTED: "execution",
            LoopState.ATTRIBUTING: "attribution",
            LoopState.ATTRIBUTED: "attribution",
            LoopState.OPTIMIZING: "optimization",
            LoopState.OPTIMIZED: "optimization",
            LoopState.VERIFYING: "verification",
            LoopState.VERIFIED: "verification",
            LoopState.RELEASING: "release",
            LoopState.COMPLETED: "complete",
        }
        return _map.get(self, "terminal")


# ══════════════════════════════════════════════════════════════════
# Transition table: which states can follow which
# ══════════════════════════════════════════════════════════════════

ALLOWED_TRANSITIONS: dict[LoopState, tuple[LoopState, ...]] = {
    LoopState.CANDIDATE:    (LoopState.EXECUTING, LoopState.ERROR),
    LoopState.EXECUTING:    (LoopState.EXECUTED, LoopState.ERROR),
    LoopState.EXECUTED:     (LoopState.ATTRIBUTING, LoopState.OPTIMIZING, LoopState.REJECTED),
    LoopState.ATTRIBUTING:  (LoopState.ATTRIBUTED, LoopState.ERROR),
    LoopState.ATTRIBUTED:   (LoopState.OPTIMIZING,),
    LoopState.OPTIMIZING:   (LoopState.OPTIMIZED, LoopState.ERROR),
    LoopState.OPTIMIZED:    (LoopState.VERIFYING,),
    LoopState.VERIFYING:    (LoopState.VERIFIED, LoopState.REJECTED, LoopState.ERROR),
    LoopState.VERIFIED:     (LoopState.RELEASING, LoopState.REJECTED),
    LoopState.RELEASING:    (LoopState.COMPLETED, LoopState.ROLLED_BACK, LoopState.ERROR),
    LoopState.COMPLETED:    (),
    LoopState.REJECTED:     (),
    LoopState.ROLLED_BACK:  (),
    LoopState.ERROR:        (),
}


@dataclass
class StateTransition:
    """A single state change event."""
    from_state: LoopState
    to_state: LoopState
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""

    def __init__(self, from_state: LoopState, to_state: LoopState):
        allowed = ALLOWED_TRANSITIONS.get(from_state, ())
        self.from_state = from_state
        self.to_state = to_state
        self.allowed = allowed
        super().__init__(f"Cannot transition {from_state.value} -> {to_state.value}. Allowed: {[s.value for s in allowed]}")


class LoopStateMachine:
    """11-state state machine for a loop orchestrator cycle.

    Enforces valid transitions and records full history.
    """

    def __init__(self, cycle_id: str):
        self.cycle_id = cycle_id
        self._state = LoopState.CANDIDATE
        self._history: list[StateTransition] = []
        self._history.append(StateTransition(
            from_state=LoopState.CANDIDATE,
            to_state=LoopState.CANDIDATE,
            reason="Cycle created",
        ))
        self._deadline: str | None = None

    # ── Properties ─────────────────────────────────────────────

    @property
    def current(self) -> LoopState:
        return self._state

    @property
    def history(self) -> list[StateTransition]:
        return list(self._history)

    @property
    def phase(self) -> str:
        return self._state.phase

    @property
    def is_terminal(self) -> bool:
        return self._state.is_terminal

    @property
    def is_active(self) -> bool:
        return self._state.is_active

    @property
    def transition_count(self) -> int:
        # Exclude the initial creation entry
        return len(self._history) - 1

    # ── Transition API ─────────────────────────────────────────

    def transition(self, to_state: LoopState, *, reason: str = "", metadata: dict[str, Any] | None = None) -> StateTransition:
        """Execute a state transition.

        Raises InvalidTransition if the move is not allowed.
        """
        allowed = ALLOWED_TRANSITIONS.get(self._state, ())
        if to_state not in allowed:
            raise InvalidTransition(self._state, to_state)

        t = StateTransition(
            from_state=self._state,
            to_state=to_state,
            reason=reason,
            metadata=metadata or {},
        )
        self._state = to_state
        self._history.append(t)
        return t

    def can_transition(self, to_state: LoopState) -> bool:
        """Check if a transition is allowed without executing it."""
        allowed = ALLOWED_TRANSITIONS.get(self._state, ())
        return to_state in allowed

    def allowed_next(self) -> tuple[LoopState, ...]:
        """Return all states that can be transitioned to from current state."""
        return ALLOWED_TRANSITIONS.get(self._state, ())

    # ── Convenience methods ────────────────────────────────────

    def start_execution(self) -> StateTransition:
        return self.transition(LoopState.EXECUTING, reason="Starting agent execution")

    def finish_execution(self, gate_passed: bool, **meta) -> StateTransition:
        return self.transition(LoopState.EXECUTED, reason=f"Gates {'passed' if gate_passed else 'failed'}", metadata=meta)

    def start_attribution(self) -> StateTransition:
        return self.transition(LoopState.ATTRIBUTING, reason="Starting causal attribution")

    def finish_attribution(self, **meta) -> StateTransition:
        return self.transition(LoopState.ATTRIBUTED, reason="Attribution complete", metadata=meta)

    def start_optimization(self) -> StateTransition:
        return self.transition(LoopState.OPTIMIZING, reason="Starting optimization analysis")

    def finish_optimization(self, **meta) -> StateTransition:
        return self.transition(LoopState.OPTIMIZED, reason="Optimization complete", metadata=meta)

    def start_verification(self) -> StateTransition:
        return self.transition(LoopState.VERIFYING, reason="Starting replay + benchmark verification")

    def finish_verification(self, passed: bool, **meta) -> StateTransition:
        if not passed:
            return self.transition(LoopState.REJECTED, reason="Verification failed", metadata=meta)
        return self.transition(LoopState.VERIFIED, reason="Verification passed", metadata=meta)

    def start_release(self) -> StateTransition:
        return self.transition(LoopState.RELEASING, reason="Starting release process")

    def finish_release(self, success: bool, **meta) -> StateTransition:
        if success:
            return self.transition(LoopState.COMPLETED, reason="Release completed", metadata=meta)
        return self.transition(LoopState.ROLLED_BACK, reason="Release rolled back", metadata=meta)

    def reject(self, reason: str, **meta) -> StateTransition:
        return self.transition(LoopState.REJECTED, reason=reason, metadata=meta)

    def error(self, reason: str, **meta) -> StateTransition:
        return self.transition(LoopState.ERROR, reason=reason, metadata=meta)

    # ── Skip optimization when gates passed ────────────────────

    def skip_to_optimization(self) -> StateTransition:
        """Skip attribution phase (used when all gates passed)."""
        return self.transition(LoopState.OPTIMIZING, reason="Gates passed, skipping attribution")

    # ── Representation ─────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "current": self._state.value,
            "phase": self.phase,
            "is_terminal": self.is_terminal,
            "transition_count": self.transition_count,
            "history": [
                {
                    "from": t.from_state.value,
                    "to": t.to_state.value,
                    "timestamp": t.timestamp,
                    "reason": t.reason,
                }
                for t in self._history
            ],
        }

    def __repr__(self) -> str:
        return f"LoopStateMachine({self.cycle_id}, state={self._state.value}, transitions={self.transition_count})"
