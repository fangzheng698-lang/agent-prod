# Demo Recording Guide

The strongest demo is a short terminal recording:

1. Start the server.
2. Submit one trace.
3. Show stats and the gate decision.

## Scripted Flow

Use this flow for a 15-second GIF or asciinema recording:

```bash
agent-prod serve --no-watchdog
python examples/basic_trace.py
agent-prod stats
agent-prod stats --detail example-basic-trace
```

If you use asciinema:

```bash
asciinema rec docs/assets/agent-prod-demo.cast
```

Then run the commands above in the recording shell.

## What The Demo Should Show

- The server starts cleanly.
- A trace is submitted with `examples/basic_trace.py`.
- The result includes Gate0-Gate7 output.
- `agent-prod stats` shows recent evaluation status.

## Suggested Caption

```text
agent-prod in 15 seconds: start the quality gate server, submit an AI agent
trace, and inspect whether the run is approved for production.
```
