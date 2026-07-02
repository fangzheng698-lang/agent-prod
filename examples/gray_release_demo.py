"""Submit a candidate version for gray release evaluation."""

from __future__ import annotations

from pprint import pprint

from agent_prod import trace


def main() -> None:
    try:
        result = trace(
            agent="example-gray-release-agent",
            version="candidate-v1.1.0",
            session_id="example-gray-release",
            decisions=[
                {
                    "decision_id": "d1",
                    "model": "gpt-4",
                    "prompt_tokens": 180,
                    "completion_tokens": 90,
                    "tool_calls": [],
                }
            ],
            current_metrics={
                "latency_p95_ms": 350,
                "success_rate": 0.995,
                "final_response": "Candidate release passed initial checks.",
            },
            traffic_percentage=10,
            human_approver="release-manager",
            policy_tags=["gray-release", "candidate"],
        )
    except ConnectionError as exc:
        print(f"Cannot reach agent-prod. Start it with `agent-prod serve`. {exc}")
        return

    pprint(result)


if __name__ == "__main__":
    main()
