from pathlib import Path
from unittest.mock import patch

from agent_prod.integration.qclaw_watchdog import QClawWatchdog


def _trace_without_approver():
    return {
        "agent": "qclaw",
        "session_id": "session-1",
        "decisions": [],
        "output": {},
        "metadata": {},
    }


def test_qclaw_watchdog_does_not_auto_approve_by_default(tmp_path):
    watchdog = QClawWatchdog(sessions_dir=tmp_path)
    submitted = {}

    with (
        patch(
            "agent_prod.integration.qclaw_watchdog.parse_qclaw_session",
            return_value=_trace_without_approver(),
        ),
        patch.object(
            watchdog,
            "_submit_evaluate",
            side_effect=lambda trace: submitted.setdefault("trace", trace)
            or {"status": "production", "passed": True},
        ),
    ):
        watchdog._process(Path("session.jsonl"))

    assert "human_approver" not in submitted["trace"]


def test_qclaw_watchdog_auto_approves_when_enabled(tmp_path):
    watchdog = QClawWatchdog(
        sessions_dir=tmp_path,
        auto_approve_missing_human_approver=True,
        auto_approver="qclaw-auto",
    )
    submitted = {}

    with (
        patch(
            "agent_prod.integration.qclaw_watchdog.parse_qclaw_session",
            return_value=_trace_without_approver(),
        ),
        patch.object(
            watchdog,
            "_submit_evaluate",
            side_effect=lambda trace: submitted.setdefault("trace", trace)
            or {"status": "production", "passed": True},
        ),
    ):
        watchdog._process(Path("session.jsonl"))

    assert submitted["trace"]["human_approver"] == "qclaw-auto"
