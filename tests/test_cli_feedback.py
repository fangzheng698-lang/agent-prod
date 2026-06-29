"""Tests for agent-prod feedback CLI command."""

from __future__ import annotations

from agent_prod.cli_feedback import _display_feedback_list, _display_feedback_detail


class TestDisplayFeedbackList:
    def test_with_improvements(self, capsys):
        improvements = {
            "imp-001": {
                "name": "Increase token budget for qclaw",
                "status": "applied",
                "metadata": {"agent": "qclaw"},
                "gate_results": [{"gate_name": "Gate6", "passed": True, "score": 0.92}],
            },
            "imp-002": {
                "name": "Add tool call frequency monitor",
                "status": "candidate",
                "metadata": {"agent": "claude-code"},
                "gate_results": [{"gate_name": "Gate6", "passed": False, "score": 0.67}],
            },
        }
        _display_feedback_list(improvements)
        captured = capsys.readouterr()
        assert "imp-001" in captured.out
        assert "imp-002" in captured.out
        assert "qclaw" in captured.out
        assert "claude-code" in captured.out
        assert "applied" in captured.out
        assert "candidate" in captured.out

    def test_empty(self, capsys):
        _display_feedback_list({})
        captured = capsys.readouterr()
        assert "No improvements found" in captured.out

    def test_without_metadata(self, capsys):
        improvements = {
            "imp-003": {
                "name": "Simple improvement",
                "status": "pending",
                "gate_results": [{"gate_name": "Gate0", "score": 0.5}],
            },
        }
        _display_feedback_list(improvements)
        captured = capsys.readouterr()
        assert "imp-003" in captured.out


class TestDisplayFeedbackDetail:
    def test_shows_all_fields(self, capsys):
        imp = {
            "name": "Test improvement",
            "status": "production",
            "metadata": {
                "agent": "qclaw",
                "session_id": "sess-abc",
                "source": "qclaw_watchdog",
            },
            "output": {
                "final_response": "This is a test response for the improvement detail display.",
            },
            "gate_results": [
                {"gate_name": "Gate0", "passed": True, "score": None, "details": "OK"},
                {"gate_name": "Gate1", "passed": True, "score": 0.85, "details": "Within budget"},
                {"gate_name": "Gate6", "passed": True, "score": 0.92, "details": "Good quality"},
            ],
        }
        _display_feedback_detail("imp-detail-1", imp)
        captured = capsys.readouterr()
        assert "imp-detail-1" in captured.out
        assert "Test improvement" in captured.out
        assert "qclaw" in captured.out
        assert "Gate0" in captured.out
        assert "Gate6" in captured.out
        assert "PASS" in captured.out

    def test_with_minimal_data(self, capsys):
        _display_feedback_detail("imp-empty", {"name": "", "status": "", "gate_results": []})
        captured = capsys.readouterr()
        assert "imp-empty" in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])