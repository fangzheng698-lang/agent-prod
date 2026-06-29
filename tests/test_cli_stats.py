"""Tests for agent-prod stats CLI command."""

from __future__ import annotations

from agent_prod.cli_stats import _display_stats_summary, _display_evaluation_detail


class TestDisplayStatsSummary:
    def test_with_full_data(self, capsys):
        stats = {
            "total": 48,
            "by_status": {"production": 5, "rejected": 43},
            "recent": [
                {
                    "id": "imp-test-1",
                    "agent": "qclaw",
                    "status": "production",
                    "gates_passed": 7,
                    "gates_total": 7,
                    "created_at": "2026-06-28T10:00:00",
                },
                {
                    "id": "imp-test-2",
                    "agent": "claude-code",
                    "status": "rejected",
                    "gates_passed": 3,
                    "gates_total": 7,
                    "created_at": "2026-06-28T11:00:00",
                },
            ],
        }
        _display_stats_summary(stats)
        captured = capsys.readouterr()
        assert "Total submissions: 48" in captured.out
        assert "production" in captured.out
        assert "rejected" in captured.out
        assert "qclaw" in captured.out
        assert "claude-code" in captured.out
        assert "imp-test-1" in captured.out

    def test_with_empty_data(self, capsys):
        _display_stats_summary({"total": 0, "by_status": {}, "recent": []})
        captured = capsys.readouterr()
        assert "Total submissions: 0" in captured.out

    def test_with_single_agent(self, capsys):
        stats = {
            "total": 3,
            "by_status": {"production": 3},
            "recent": [
                {
                    "id": "imp-agent-1",
                    "agent": "hermes",
                    "status": "production",
                    "gates_passed": 7,
                    "gates_total": 7,
                    "created_at": "2026-06-28T12:00:00",
                },
            ],
        }
        _display_stats_summary(stats)
        captured = capsys.readouterr()
        assert "hermes" in captured.out
        assert "100.0%" in captured.out


class TestDisplayEvaluationDetail:
    def test_shows_all_fields(self, capsys):
        imp = {
            "id": "imp-abc-123",
            "agent": "qclaw",
            "status": "production",
            "gates_passed": 7,
            "gates_total": 7,
            "created_at": "2026-06-28T10:00:00",
        }
        _display_evaluation_detail(imp)
        captured = capsys.readouterr()
        assert "imp-abc-123" in captured.out
        assert "qclaw" in captured.out
        assert "production" in captured.out
        assert "7/7" in captured.out

    def test_with_missing_fields(self, capsys):
        _display_evaluation_detail({"id": "imp-minimal"})
        captured = capsys.readouterr()
        assert "imp-minimal" in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])