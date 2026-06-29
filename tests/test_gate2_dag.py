"""Tests for Gate2 local DAG validator (_check_local_dag)."""
from __future__ import annotations

from agent_prod.gates.gate2_trace import Gate2TraceIntegrity
from agent_prod.gates.models import Improvement


class TestGate2LocalDAG:
    """Direct tests for _check_local_dag without Jaeger/OTel."""

    def test_valid_dag(self):
        """Well-formed LLM→tool graph passes."""
        imp = Improvement(
            name="valid-dag",
            llm_calls=[
                {"response_id": "r1", "duration_ms": 100},
                {"response_id": "r2", "duration_ms": 200},
            ],
            tool_calls=[
                {"request_id": "r1", "tool": "read"},
                {"request_id": "r2", "tool": "write"},
            ],
        )
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert details["valid"]
        assert details["source"] == "local_dag"

    def test_orphan_tool_call(self):
        """Tool call with no matching LLM response_id is detected."""
        imp = Improvement(
            name="orphan-tool",
            llm_calls=[{"response_id": "r1", "duration_ms": 100}],
            tool_calls=[{"request_id": "orphan_x", "tool": "bad_tool"}],
        )
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert not details["valid"]
        assert len(details["orphan_tool_calls"]) == 1
        assert "orphan_x" in details["orphan_tool_calls"][0]

    def test_orphan_llm_call(self):
        """LLM call whose response_id isn't referenced by any tool call."""
        imp = Improvement(
            name="orphan-llm",
            llm_calls=[
                {"response_id": "r1", "duration_ms": 100},
                {"response_id": "r2", "duration_ms": 200},
            ],
            tool_calls=[{"request_id": "r1", "tool": "read"}],
        )
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert not details["valid"]
        assert len(details["orphan_llm_calls"]) == 1
        assert "r2" in details["orphan_llm_calls"][0]

    def test_no_tool_calls_no_orphan_llm(self):
        """When there are no tool calls, LLM calls aren't flagged as orphan."""
        imp = Improvement(
            name="no-tools",
            llm_calls=[{"response_id": "r1", "duration_ms": 100}],
            tool_calls=[],
        )
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert details["valid"]
        assert len(details["orphan_llm_calls"]) == 0

    def test_unterminated_llm_call(self):
        """LLM call without duration_ms or finish_reason is flagged."""
        imp = Improvement(
            name="unterminated",
            llm_calls=[{"response_id": "r1"}],  # no duration_ms, no finish_reason
            tool_calls=[],
        )
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert not details["valid"]
        assert len(details["unterminated_llm_calls"]) == 1

    def test_finish_reason_avoids_unterminated(self):
        """LLM call with finish_reason but no duration_ms is NOT flagged."""
        imp = Improvement(
            name="finish-reason-ok",
            llm_calls=[{"response_id": "r1", "finish_reason": "stop"}],
            tool_calls=[],
        )
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert details["valid"]
        assert len(details["unterminated_llm_calls"]) == 0

    def test_empty_calls_passes(self):
        """No LLM calls and no tool calls is valid."""
        imp = Improvement(name="empty", llm_calls=[], tool_calls=[])
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert details["valid"]

    def test_cycle_detected(self):
        """LLM→Tool→LLM cycle where tool response_id matches LLM request_id."""
        imp = Improvement(
            name="cycle-test",
            llm_calls=[
                {"response_id": "resp-1", "request_id": "tool-resp-1", "duration_ms": 100},
            ],
            tool_calls=[
                {"request_id": "resp-1", "response_id": "tool-resp-1", "tool": "search"},
            ],
        )
        details = Gate2TraceIntegrity._check_local_dag(imp)
        assert not details["valid"]
        assert len(details["cycles"]) > 0

    def test_verify_integration(self):
        """Gate2TraceIntegrity.verify() uses _check_local_dag as fallback."""
        gate2 = Gate2TraceIntegrity()
        imp = Improvement(
            name="verify-test",
            llm_calls=[{"response_id": "r1", "duration_ms": 100}],
            tool_calls=[{"request_id": "r1", "tool": "read"}],
        )
        result = gate2.verify(imp)
        assert result.passed
        assert result.details.get("source") == "local_dag"

    def test_verify_detects_orphan(self):
        """verify() returns not-passed for orphan tool calls."""
        gate2 = Gate2TraceIntegrity()
        imp = Improvement(
            name="verify-orphan",
            llm_calls=[{"response_id": "r1", "duration_ms": 100}],
            tool_calls=[{"request_id": "orphan", "tool": "bad"}],
        )
        result = gate2.verify(imp)
        assert not result.passed
        assert "orphan" in result.reason.lower()