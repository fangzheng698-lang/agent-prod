#!/bin/bash
# claude-proxy — wrapper script that starts Claude Code with agent-prod monitoring
#
# Usage:
#   claude-proxy           # Start Claude Code with proxy monitoring
#   claude-proxy <args>    # Pass arguments through to Claude Code
#
# This wrapper:
#   1. Registers a new session with agent-prod
#   2. Starts Claude Code with ANTHROPIC_BASE_URL pointing to agent-prod
#   3. When Claude Code exits, sends end-of-session signal
#
# Requirements:
#   - agent-prod server running at http://localhost:8765
#   - Claude Code CLI installed at the default path

AGENT_PROD_URL="${AGENT_PROD_URL:-http://localhost:8765}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
SESSION_ID="pxy_$(uuidgen | tr '[:upper:]' '[:lower:]' | tr -d '-' | head -c 12)"
AGENT_TYPE="${AGENT_TYPE:-claude-code}"

# Register session with agent-prod
curl -s -X POST "$AGENT_PROD_URL/v1/proxy/register" \
  -H "Content-Type: application/json" \
  -d "{
    \"agent\": \"$AGENT_TYPE\",
    \"session_id\": \"$SESSION_ID\",
    \"declared_tools\": [\"Read\", \"Write\", \"Edit\", \"Bash\", \"Agent\", \"TaskCreate\", \"Think\"],
    \"version\": \"2.1.150\"
  }" > /dev/null 2>&1

echo "agent-prod: monitoring session $SESSION_ID"

# Start Claude Code with proxy base URL
# Note: We set ANTHROPIC_BASE_URL so outgoing LLM calls go through agent-prod
# The proxy endpoint forwards them to the real upstream
ANTHROPIC_BASE_URL="$AGENT_PROD_URL/v1/proxy" \
  "$CLAUDE_BIN" "$@" --session-id "$SESSION_ID"

EXIT_CODE=$?

# Send end-of-session signal
curl -s -X POST "$AGENT_PROD_URL/v1/proxy/heartbeat" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"status\": \"completed\"}" > /dev/null 2>&1

echo "agent-prod: session $SESSION_ID completed (exit code: $EXIT_CODE)"
exit $EXIT_CODE