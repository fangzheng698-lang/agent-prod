"""LoopStateMachine unit tests."""
import pytest
from agent_prod.lifecycle.loop_state import (
    LoopStateMachine, LoopState, ALLOWED_TRANSITIONS, InvalidTransition,
)


def test_initial_state_is_candidate():
    sm = LoopStateMachine("test")
    assert sm.current == LoopState.CANDIDATE


def test_transition_count_starts_at_zero():
    sm = LoopStateMachine("test")
    assert sm.transition_count == 0


def test_start_execution():
    sm = LoopStateMachine("test")
    sm.start_execution()
    assert sm.current == LoopState.EXECUTING


def test_full_happy_path():
    sm = LoopStateMachine("happy")
    sm.start_execution()
    sm.finish_execution(gate_passed=True)
    sm.skip_to_optimization()  # gates passed, skip attribution
    sm.finish_optimization()
    sm.start_verification()
    sm.finish_verification(passed=True)
    sm.start_release()
    sm.finish_release(success=True)
    assert sm.current == LoopState.COMPLETED
    assert sm.transition_count == 8


def test_gate_failure_path():
    sm = LoopStateMachine("fail")
    sm.start_execution()
    sm.finish_execution(gate_passed=False)
    sm.start_attribution()
    sm.finish_attribution()
    sm.start_optimization()
    sm.finish_optimization()
    sm.start_verification()
    sm.finish_verification(passed=False)
    assert sm.current == LoopState.REJECTED


def test_error_from_executing():
    sm = LoopStateMachine("err")
    sm.start_execution()
    sm.error("Something broke")
    assert sm.current == LoopState.ERROR
    assert sm.is_terminal


def test_cannot_transition_from_terminal():
    sm = LoopStateMachine("term")
    sm.start_execution()
    sm.error("dead")
    with pytest.raises(InvalidTransition):
        sm.start_optimization()


def test_invalid_transition_raises():
    sm = LoopStateMachine("bad")
    with pytest.raises(InvalidTransition):
        sm.transition(LoopState.COMPLETED)


def test_completed_is_terminal():
    sm = LoopStateMachine("done")
    sm.start_execution()
    sm.finish_execution(True)
    sm.skip_to_optimization()
    sm.finish_optimization()
    sm.start_verification()
    sm.finish_verification(True)
    sm.start_release()
    sm.finish_release(True)
    assert sm.is_terminal


def test_rejected_is_terminal():
    sm = LoopStateMachine("rej")
    sm.start_execution()
    sm.finish_execution(True)
    sm.skip_to_optimization()
    sm.finish_optimization()
    sm.start_verification()
    sm.finish_verification(passed=False)  # rejects
    assert sm.is_terminal
    assert sm.current == LoopState.REJECTED


def test_rollback_path():
    sm = LoopStateMachine("rb")
    sm.start_execution()
    sm.finish_execution(True)
    sm.skip_to_optimization()
    sm.finish_optimization()
    sm.start_verification()
    sm.finish_verification(True)
    sm.start_release()
    sm.finish_release(False)
    assert sm.current == LoopState.ROLLED_BACK


def test_transition_with_good_metadata():
    sm = LoopStateMachine("meta")
    sm.start_execution()
    t = sm.finish_execution(True, tokens=100, time_ms=500)
    assert t.metadata == {"tokens": 100, "time_ms": 500}


def test_allowed_next():
    sm = LoopStateMachine("next")
    allowed = sm.allowed_next()
    assert LoopState.EXECUTING in allowed
    assert LoopState.ERROR in allowed
    assert LoopState.COMPLETED not in allowed


def test_to_dict():
    sm = LoopStateMachine("dict")
    sm.start_execution()
    sm.finish_execution(True)
    d = sm.to_dict()
    assert d["cycle_id"] == "dict"
    assert d["current"] == "executed"
    assert d["transition_count"] == 2
    assert len(d["history"]) == 3  # creation + 2 transitions


def test_phase_property():
    sm = LoopStateMachine("phase")
    assert sm.phase == "init"
    sm.start_execution()
    assert sm.phase == "execution"
    sm.finish_execution(True)
    assert sm.phase == "execution"
    sm.skip_to_optimization()
    assert sm.phase == "optimization"
