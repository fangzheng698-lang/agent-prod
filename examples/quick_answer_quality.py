"""Evaluate answer quality with the quick helper."""

from __future__ import annotations

from pprint import pprint

from agent_prod import quick


def main() -> None:
    try:
        result = quick(
            final_response="agent-prod is a production quality gate for AI agents.",
            expected_answer="agent-prod is a production quality gate for AI agents.",
            agent="example-answer-quality",
            session_id="example-answer-quality",
        )
    except ConnectionError as exc:
        print(f"Cannot reach agent-prod. Start it with `agent-prod serve`. {exc}")
        return

    pprint(result)


if __name__ == "__main__":
    main()
