# agent-prod Examples

Run these examples after starting the local service:

```bash
agent-prod serve
```

By default the SDK sends requests to `http://localhost:8000`. Override it with:

```bash
export AGENT_PROD_URL=http://localhost:8000
```

Examples:

- `basic_trace.py` — submit a normal agent trace through Gate0-Gate7.
- `quick_answer_quality.py` — evaluate final answer quality with the `quick()` helper.
- `regression_detection_demo.py` — compare current metrics with a baseline to demonstrate regression detection.
- `gray_release_demo.py` — submit a candidate version with a traffic percentage and approver.

These examples are intentionally small so you can copy the payload shape into
your own agent runtime.
