"""Submit a minimal agent trace to agent-prod.

Start the service first:
    agent-prod serve
"""

from __future__ import annotations

from pprint import pprint

from agent_prod import trace


def main() -> None:
    try:
        result = trace(
            agent="example-agent",
            version="v1.0.0",
            session_id="example-basic-trace",
            declared_tools=["search"],
            decisions=[
                {
                    "decision_id": "d1",
                    "model": "gpt-4",
                    "prompt_tokens": 120,
                    "completion_tokens": 64,
                    "tool_calls": [
                        {
                            "tool_id": "t1",
                            "tool_name": "search",
                            "arguments": {"query": "agent quality gate"},
                            "result_summary": "Found relevant documentation.",
                            "success": True,
                            "duration_ms": 180.0,
                        }
                    ],
                }
            ],
            current_metrics={
                "final_response": "agent-prod validates agent runs before production.",
                "expected_answer": "agent-prod validates agent runs before production.",
                "latency_p95_ms": 420,
                "success_rate": 0.99,
            },
            traffic_percentage=1,
            human_approver="demo",
        )
    except ConnectionError as exc:
        print(f"Cannot reach agent-prod. Start it with `agent-prod serve`. {exc}")
        return

    pprint(result)


if __name__ == "__main__":
    main()
