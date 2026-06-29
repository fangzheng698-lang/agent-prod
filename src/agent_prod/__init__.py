"""agent-prod — Enterprise-grade AI agent quality gate infrastructure.

Quick start for any agent (one line of code):

    from agent_prod import trace

    result = trace(
        agent="my-custom-agent",
        session_id="ses_001",
        decisions=[{
            "decision_id": "d1",
            "model": "gpt-4",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "tool_calls": [{
                "tool_id": "t1",
                "tool_name": "search",
                "arguments": {"query": "weather"},
                "result_summary": "Sunny, 22C",
                "success": True,
                "duration_ms": 120.0,
            }],
        }],
        current_metrics={
            "latency_p95_ms": 300,
            "success_rate": 0.99,
            "final_response": "Sunny, 22C",
        },
        traffic_percentage=100,
    )

    if result["passed"]:
        print("All gates passed -> production")
    else:
        print(f"Rejected at {result['failed_at']}: {result['fail_reason']}")
"""

__version__ = "0.5.0"

# Public SDK
from agent_prod.client import AgentProdClient, AgentProdError, to_agent_trace  # noqa: F401
from agent_prod.trace_client import trace, quick, health, evaluate_batch  # noqa: F401

__all__ = [
    "AgentProdClient", "AgentProdError", "to_agent_trace",
    "trace", "quick", "health", "evaluate_batch",
]