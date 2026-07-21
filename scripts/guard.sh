#!/usr/bin/env bash
# guard.sh — agent-prod server + qclaw_watchdog 守卫脚本
# 用法: bash scripts/guard.sh
# 由 cron 每 5 分钟调用。检查端口和进程，异常时自动重启。
#
# macOS cron 注意事项:
#   - Desktop 路径可能需要 Terminal "完全磁盘访问权限"
#   - 如果 cron 无法读取 ~/Desktop，将本脚本移到 ~/guard.sh 并修改 REPO_ROOT
#
# Log: logs/guard.log

set -u

REPO_ROOT="/Users/qz/Desktop/agent-prod"
cd "$REPO_ROOT" || exit 1

PYTHON="$REPO_ROOT/.venv/bin/python"
LOG="$REPO_ROOT/logs/guard.log"
PORT=9002

WD_PAT="agent_prod.integration.qclaw_watchdog"
SRV_CMD="$PYTHON -m uvicorn agent_prod.server.app:app --host 0.0.0.0 --port $PORT --log-level warning"
WD_CMD="$PYTHON -m agent_prod.integration.qclaw_watchdog --url http://localhost:$PORT --interval 5 --auto-approve-missing-human-approver"

ts()  { date '+%Y-%m-%d %H:%M:%S%z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# ── 健康检查（纯 Python，无外部依赖）─────────────────────
check_port_alive() {
    "$PYTHON" - "$PORT" <<'PY'
import socket, sys
p = int(sys.argv[1])
try:
    s = socket.create_connection(("127.0.0.1", p), timeout=3)
    s.close(); print("alive")
except Exception:
    print("dead")
PY
}

# ── Server ─────────────────────────────────────────────────
port_state=$(check_port_alive)
srv_pid=$(pgrep -f "uvicorn.*agent_prod.server.app:app.*--port $PORT" | head -1 || true)

if [ "$port_state" != "alive" ]; then
    if [ -n "$srv_pid" ]; then
        log "server pid=$srv_pid alive? no — stale, killing"
        kill -TERM "$srv_pid" 2>/dev/null; sleep 1
        kill -KILL "$srv_pid" 2>/dev/null || true
    fi
    log "server DOWN — restarting"
    nohup $SRV_CMD > "$REPO_ROOT/logs/server.log" 2>&1 &
    log "server restarted, pid=$!"
elif [ -z "$srv_pid" ]; then
    # port alive but no uvicorn pid in pgrep — might be another service on 9002; warn
    log "WARN: port $PORT alive but no uvicorn process detected — skipping server restart"
fi

# ── Watchdog ───────────────────────────────────────────────
wd_pid=$(pgrep -f "$WD_PAT" | head -1 || true)
if [ -z "$wd_pid" ]; then
    log "watchdog DOWN — restarting"
    nohup $WD_CMD > "$REPO_ROOT/logs/watchdog.log" 2>&1 &
    log "watchdog restarted, pid=$!"
fi
