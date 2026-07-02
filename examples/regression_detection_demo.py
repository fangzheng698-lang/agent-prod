"""Demonstrate regression-oriented metrics in a trace payload."""

from __future__ import annotations

from pprint import pprint

from agent_prod import trace


def main() -> None:
    try:
        result = trace(
            agent="example-regression-agent",
            version="candidate-v2",
            session_id="example-regression-detection",
            decisions=[
                {
                    "decision_id": "d1",
                    "model": "gpt-4",
                    "prompt_tokens": 300,
                    "completion_tokens": 140,
                    "tool_calls": [],
                }
            ],
            baseline_metrics={
                "latency_p95_ms": 500,
                "success_rate": 0.99,
                "token_cost": 0.02,
            },
            current_metrics={
                "latency_p95_ms": 900,
                "success_rate": 0.92,
                "token_cost": 0.05,
                "final_response": "The candidate response is slower and less reliable.",
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
