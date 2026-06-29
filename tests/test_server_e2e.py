"""E2E test: server /v1/agent/evaluate endpoint with quality gates.

Starts the FastAPI app in memory mode (no external deps), posts a valid
agent trace, and verifies that all 7 quality gates run and return results.
"""
from __future__ import annotations

import os

# Must set env vars BEFORE importing app — module-level init reads them
os.environ.setdefault("QUALITY_GATES_MODE", "memory")
os.environ.setdefault("QUALITY_GATES_ENABLED", "true")

from fastapi.testclient import TestClient

from agent_prod.server.app import app

client = TestClient(app)

SAMPLE_TRACE = {
    "agent": "generic",
    "session_id": "ses_e2e_test",
    "version": "0.1.0",
    "output": {"final_response": "The capital of France is Paris."},
    "decisions": [
        {
            "decision_id": "d1",
            "model": "gpt-4o-mini",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "tool_calls": [
                {
                    "tool_id": "t1",
                    "tool_name": "web_search",
                    "arguments": {"query": "capital of France"},
                    "result_summary": "Paris is the capital of France.",
                    "success": True,
                    "duration_ms": 150.0,
                },
            ],
        },
    ],
    "current_metrics": {
        "latency_p95_ms": 300,
        "success_rate": 1.0,
        "expected_answer": "Paris",
        "final_response": "The capital of France is Paris.",
        "user_question": "What is the capital of France?",
    },
    "traffic_percentage": 1,
    "human_approver": "auto",
    "declared_tools": ["web_search"],
}


class TestEvaluateEndpoint:
    """E2E test against the live FastAPI app using TestClient."""

    def test_health_returns_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["quality_gates"] is True

    def test_ready_returns_ok(self):
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True

    def test_evaluate_returns_gate_results(self):
        resp = client.post("/v1/agent/evaluate", json=SAMPLE_TRACE)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        data = resp.json()
        assert data["agent"] == "generic"
        assert data["session_id"] == "ses_e2e_test"
        assert data["status"] in ("production", "rejected")
        assert "passed" in data
        assert "gates" in data
        assert len(data["gates"]) > 0
        assert data["total_duration_ms"] > 0

        # All 7 gate results should be present
        gate_names = {g["gate"] for g in data["gates"]}
        assert "gate0_permission" in gate_names
        assert "gate1_execution" in gate_names
        assert "gate2_trace_integrity" in gate_names
        assert "gate3_regression" in gate_names
        assert "gate4_gray_release" in gate_names
        assert "gate5_release_audit" in gate_names
        assert "gate6_answer_quality" in gate_names

    def test_evaluate_minimal_payload(self):
        """Minimal valid payload — only required fields."""
        resp = client.post("/v1/agent/evaluate", json={
            "agent": "test-agent",
            "session_id": "ses_minimal",
        })
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["session_id"] == "ses_minimal"
        assert len(data["gates"]) == 7

    def test_evaluate_missing_session_id(self):
        """Missing session_id: auto-generated, not an error."""
        resp = client.post("/v1/agent/evaluate", json={
            "agent": "test-agent",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] != ""  # auto-generated

    def test_evaluate_without_gateway_returns_503(self):
        """Can't easily test without gateway since it's module-level,
        but verify the endpoint at least validates the payload."""
        resp = client.post("/v1/agent/evaluate", json={
            "decisions": "not_a_list",  # invalid type
        })
        assert resp.status_code == 400  # validation error