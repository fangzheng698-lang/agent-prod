#!/usr/bin/env bash
set -euo pipefail

echo "$ agent-prod serve --no-watchdog"
echo "Starting agent-prod on http://localhost:8000"
echo
echo "$ python examples/basic_trace.py"
python examples/basic_trace.py
echo
echo "$ agent-prod stats"
agent-prod stats
