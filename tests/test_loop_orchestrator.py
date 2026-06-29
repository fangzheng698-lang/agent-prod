"""LoopOrchestrator unit and integration tests."""
from __future__ import annotations

import tempfile
import os

from agent_prod.adaptivity.loop_orchestrator import (
    LoopOrchestrator, CycleResult, CyclePhase,
)


def _make_orch(log_path: str) -> LoopOrchestrator:
    return LoopOrchestrator(log_path=log_path)


# ── Batch mode (no turns) ────────────────────────────────────────

def test_sync_cycle_no_turns_completes_all_phases():
    """Batch mode: even with no turn data, all 4 phases should run."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="test prompt")

        assert isinstance(result, CycleResult)
        assert len(result.phases) == 4
        assert {p.name for p in result.phases} == {
            "execution", "attribution", "optimization", "release",
        }


def test_sync_cycle_generates_ids():
    """Session ID and run ID are auto-generated if not provided."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="test")

        assert result.session_id.startswith("sess-")
        assert result.run_id.startswith("run-")
        assert len(result.session_id) > 5
        assert len(result.run_id) > 5


def test_sync_cycle_custom_session_id():
    """Custom session_id is preserved."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="test", session_id="my-custom-session")

        assert result.session_id == "my-custom-session"


def test_sync_cycle_summary_readable():
    """summary property should be a readable multi-line string."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="summarize me")

        s = result.summary
        assert "LoopOrchestrator" in s
        assert result.run_id in s
        assert len(s) > 100


def test_sync_cycle_total_duration_positive():
    """Total duration should be positive."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="timing test")

        assert result.total_duration_ms > 0


# ── Phase correctness ────────────────────────────────────────────

def test_phases_ordered():
    """Phases must be in order: execution → attribution → optimization → release."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="order test")

        names = [p.name for p in result.phases]
        assert names == ["execution", "attribution", "optimization", "release"]


def test_attribution_skipped_when_no_turns():
    """Without turns data, attribution should be skipped (not failed)."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="attribution test")

        attr_phase = result.phases[1]
        assert attr_phase.name == "attribution"
        assert attr_phase.data.get("skipped") is True


# ── Execution log writing ────────────────────────────────────────

def test_execution_log_written():
    """Each cycle writes an execution log record."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="log test")

        assert os.path.exists(log_path)
        with open(log_path) as f:
            lines = f.read().strip().split("\n")
        assert len(lines) >= 1  # at least one record


def test_execution_log_contains_run_id():
    """Log records must contain the run_id."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="run_id in log")

        with open(log_path) as f:
            content = f.read()
        assert result.run_id in content


# ── Release Manager integration ──────────────────────────────────

def test_release_manager_creates_version():
    """Each cycle creates a release version."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="release test")

        assert result.release_state is not None
        assert result.release_state.version.startswith("v")
        assert result.release_state.status.value in ("candidate", "rolled_back")


def test_release_manager_accumulates():
    """Multiple cycles create multiple release versions."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)

        orch.run_cycle_sync(prompt="cycle 1")
        orch.run_cycle_sync(prompt="cycle 2")
        orch.run_cycle_sync(prompt="cycle 3")

        all_releases = orch.release_manager.list_releases()
        assert len(all_releases) >= 3


# ── Flywheel integration ─────────────────────────────────────────

def test_flywheel_report_generated():
    """Optimization phase generates a FlywheelReport (may need min samples)."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)
        result = orch.run_cycle_sync(prompt="flywheel test")

        # With <5 samples, report summary mentions "Need at least 5"
        # but the phase itself should succeed
        opt_phase = result.phases[2]
        assert opt_phase.name == "optimization"
        assert opt_phase.success


# ── Gateway property ─────────────────────────────────────────────

def test_gateway_auto_created():
    """Gateway is auto-created in memory mode if not injected."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = _make_orch(log_path)

        gw = orch.gateway
        assert gw is not None
        assert gw.engine is not None


def test_gateway_injectable():
    """Gateway can be injected via constructor."""
    from agent_prod.gateway.gateway import QualityGateGateway

    gw = QualityGateGateway.memory()
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "exec_log.jsonl")
        orch = LoopOrchestrator(log_path=log_path, gateway=gw)

        assert orch.gateway is gw


# ── CyclePhase model ─────────────────────────────────────────────

def test_cycle_phase_fields():
    """CyclePhase dataclass fields are accessible."""
    phase = CyclePhase(
        name="test",
        duration_ms=123.4,
        success=True,
        data={"key": "value"},
    )
    assert phase.name == "test"
    assert phase.duration_ms == 123.4
    assert phase.success is True
    assert phase.data == {"key": "value"}
    assert phase.error == ""
