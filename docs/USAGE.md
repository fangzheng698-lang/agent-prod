# Usage Guide

## Install

```bash
pip install agent-prod
```

For local development:

```bash
pip install -e ".[dev]"
```

## Quick Start

```bash
agent-prod configure
agent-prod serve
python examples/basic_trace.py
agent-prod stats
```

## One-Line SDK

```python
from agent_prod import trace

result = trace(
    agent="my-custom-agent",
    session_id="session_001",
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
        "final_response": "Sunny, 22C",
        "latency_p95_ms": 300,
        "success_rate": 0.99,
    },
)
```

## CLI

```bash
agent-prod configure
agent-prod configure --show
agent-prod configure --reset

agent-prod serve
agent-prod doctor

agent-prod stats
agent-prod stats --agent qclaw
agent-prod stats --rejected
agent-prod stats --detail <id>

agent-prod feedback
agent-prod feedback --id <id>
agent-prod feedback --apply <id>

agent-prod watch
```

## Gate0-Gate7

| Gate | Name | Purpose |
|---|---|---|
| Gate0 | Permission | Tool ACL, observe/enforce modes, risky argument detection |
| Gate1 | Budget | Token and time budget checks with circuit breaking |
| Gate2 | Trace integrity | LLM-to-tool DAG integrity |
| Gate3 | Regression | Compare against baselines and detect quality/performance regression |
| Gate4 | Gray release | Progressive rollout state |
| Gate5 | Audit | Release compliance and human approval context |
| Gate6 | Answer quality | LLM/exact/pre-scored answer quality checks |
| Gate7 | Execution consistency | Plan, goal, and output consistency |

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_PROD_URL` | `http://localhost:8000` | Server URL |
| `AGENT_PROD_API_KEY` | empty | API key for protected deployments |
| `OPENAI_API_KEY` | empty | LLM judging for Gate6 |
| `QUALITY_GATES_MODE` | `memory` | `memory` or `production` |
| `AGENT_PROD_WATCHDOG_AUTO_START` | `true` | Auto-start Hermes watchdog with the server |

## Gate0 Modes

| Mode | Behavior |
|---|---|
| `observe` | Record violations without blocking. Use for new agents. |
| `enforce` | Block risky or undeclared tool calls. Use after calibration. |

```yaml
gates:
  gate0:
    per_agent:
      my-agent:
        mode: observe
```
