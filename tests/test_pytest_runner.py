"""Pytest wrappers for the standalone test scripts.

Each script runs as a subprocess — exit code 0 means pass.
This preserves all existing test logic without rewriting 1100+ lines.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"


def _run(script: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a test script and return the CompletedProcess."""
    script_path = TESTS_DIR / script
    if not script_path.exists():
        raise FileNotFoundError(f"Test script not found: {script_path}")
    return subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(ROOT),
    )


def _assert_pass(result: subprocess.CompletedProcess, name: str) -> None:
    """Assert that a test script passed, showing output on failure."""
    if result.returncode != 0:
        stdout_tail = result.stdout[-600:] if len(result.stdout) > 600 else result.stdout
        stderr_tail = result.stderr[-600:] if len(result.stderr) > 600 else result.stderr
        raise AssertionError(
            f"{name} FAILED (exit {result.returncode})\n"
            f"--- STDOUT ---\n{stdout_tail}\n"
            f"--- STDERR ---\n{stderr_tail}"
        )


def test_phase1_real() -> None:
    """Phase 1: quality gate engine real-path tests (27 assertions)."""
    result = _run("test_phase1_real.py")
    _assert_pass(result, "test_phase1_real")


def test_phase11_causal() -> None:
    """Phase 11: causal attribution — Granger + counterfactual (12 assertions)."""
    result = _run("test_phase11_causal.py")
    _assert_pass(result, "test_phase11_causal")


def test_alerts() -> None:
    """Alert dispatch: formatting, Discord, Telegram, Webhook (4 suites)."""
    result = _run("test_alerts.py")
    _assert_pass(result, "test_alerts")


def test_per_agent_thresholds() -> None:
    """Per-agent threshold resolution (15 assertions)."""
    result = _run("test_per_agent_thresholds.py")
    _assert_pass(result, "test_per_agent_thresholds")


def test_e2e_flywheel() -> None:
    """E2E: data flywheel + adaptive gates (requires server or skips)."""
    result = _run("test_e2e_flywheel.py")
    # e2e may skip gracefully (no server) — still exit 0
    _assert_pass(result, "test_e2e_flywheel")
