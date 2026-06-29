"""Tests for Governance + Benchmark integration in LoopOrchestrator v2."""
import tempfile
import os

from agent_prod.adaptivity.loop_orchestrator import LoopOrchestrator


def _make_orch():
    return LoopOrchestrator(log_path=":memory:", replay_dir=":memory:please", benchmark_dir=":memory:please")


# ── Governance ───────────────────────────────────────────────────

def test_governance_property_exists():
    orch = _make_orch()
    assert orch.governance is not None
    assert hasattr(orch.governance, "to_text")


def test_governance_summary_in_result():
    with tempfile.TemporaryDirectory() as tmp:
        orch = LoopOrchestrator(log_path=os.path.join(tmp, "log.jsonl"))
        result = orch.run_cycle_sync(prompt="gov test")

        assert result.governance_summary is not None
        assert "total_releases" in result.governance_summary
        assert result.governance_summary["total_releases"] >= 1


def test_governance_tracks_cycles():
    with tempfile.TemporaryDirectory() as tmp:
        orch = LoopOrchestrator(log_path=os.path.join(tmp, "log.jsonl"))
        orch.run_cycle_sync(prompt="c1")
        orch.run_cycle_sync(prompt="c2")
        orch.run_cycle_sync(prompt="c3")

        gov = orch.governance
        assert gov.summary()["total_releases"] >= 3


def test_governance_to_text_readable():
    with tempfile.TemporaryDirectory() as tmp:
        orch = LoopOrchestrator(log_path=os.path.join(tmp, "log.jsonl"))
        orch.run_cycle_sync(prompt="text test")

        text = orch.governance.to_text()
        assert "Governance Panel" in text
        assert "Gray Release Status" in text
        assert len(text) > 200


# ── Benchmark ────────────────────────────────────────────────────

def test_benchmark_stores_versioned_snapshots():
    with tempfile.TemporaryDirectory() as tmp:
        orch = LoopOrchestrator(log_path=os.path.join(tmp, "log.jsonl"), benchmark_dir=os.path.join(tmp, "bench"))
        result = orch.run_cycle_sync(prompt="bench test")

        # Check that a versioned snapshot file was created
        bench_dir = os.path.join(tmp, "bench")
        files = [f for f in os.listdir(bench_dir) if f.endswith(".json")]
        assert len(files) >= 1


def test_benchmark_improved_flag_present():
    with tempfile.TemporaryDirectory() as tmp:
        orch = LoopOrchestrator(log_path=os.path.join(tmp, "log.jsonl"))
        result = orch.run_cycle_sync(prompt="b1")

        # First cycle has no comparison baseline, should be None
        assert result.benchmark_improved is None

        # Second cycle compares against first
        result2 = orch.run_cycle_sync(prompt="b2")
        # Should be True or False (compared against first snapshot)
        assert result2.benchmark_improved is not None or result2.benchmark_improved is True or result2.benchmark_improved is False


# ── Replay ───────────────────────────────────────────────────────

def test_replay_dir_set_up():
    with tempfile.TemporaryDirectory() as tmp:
        orch = LoopOrchestrator(log_path=os.path.join(tmp, "log.jsonl"), replay_dir=os.path.join(tmp, "replays"))
        result = orch.run_cycle_sync(prompt="replay test")

        # Replay dir exists even if no turns recorded
        assert os.path.exists(os.path.join(tmp, "replays"))
