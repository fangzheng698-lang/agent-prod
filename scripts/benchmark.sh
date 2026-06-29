#!/bin/bash
# agent-prod 压力测试 + benchmark
# Usage: bash scripts/benchmark.sh [--quick|--full]
#   --quick: 10 秒轻量冒烟 (默认)
#   --full:  30 秒 x 3 轮，输出 JSON 报告

set -euo pipefail

MODE="${1:---quick}"
HOST="${AGENT_PROD_HOST:-http://localhost:8765}"
REPORT_DIR="${2:-/tmp/agent-prod-benchmark}"
mkdir -p "$REPORT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[bench]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[FAIL]${NC} $*"; }

# ── 负载测试数据 ──────────────────────────────────────────
PAYLOAD_HEALTH='{}'
PAYLOAD_EVAL='{"agent":"hermes","session_id":"bench-$RANDOM","traffic_percentage":1,"decisions":[{"decision_id":"d1","model":"gpt-4","prompt_tokens":100,"completion_tokens":50,"tool_calls":[{"tool_id":"t1","tool_name":"search","arguments":{"q":"test"},"result_summary":"ok","success":true}]}],"current_metrics":{"latency_p95_ms":300,"success_rate":0.99,"expected_answer":"ok","final_response":"ok"},"human_approver":"bench"}'

# ── 1) 健康检查 ──────────────────────────────────────────
log "1/5 健康检查..."
HEALTH=$(curl -sf "$HOST/health" 2>/dev/null || echo '{"status":"down"}')
echo "$HEALTH" | python3 -c "import json,sys;d=json.load(sys.stdin);print(f'  status={d[\"status\"]} repo={d.get(\"repository\",\"?\")}')"

# ── 2) 冒烟验证 ──────────────────────────────────────────
log "2/5 功能验证..."
EVAL=$(curl -sf -X POST "$HOST/v1/agent/evaluate" \
  -H 'Content-Type: application/json' \
  -d '{"agent":"hermes","session_id":"bench-smoke","traffic_percentage":1,"decisions":[{"decision_id":"d1","model":"gpt-4","prompt_tokens":10,"completion_tokens":5,"tool_calls":[]}],"current_metrics":{"latency_p95_ms":300,"success_rate":0.99,"expected_answer":"巴黎是法国的首都","final_response":"巴黎是法国的首都"}}')
echo "$EVAL" | python3 -c "import json,sys;d=json.load(sys.stdin);print(f'  status={d[\"status\"]} gates={len(d[\"gates\"])}')"

# ── 3) Metrics 端点 ──────────────────────────────────────
log "3/5 Metrics 端点..."
METRICS=$(curl -sf "$HOST/metrics" 2>/dev/null | head -5)
echo "  $(echo "$METRICS" | wc -l) lines"

# ── 4) wrk 压力测试 ──────────────────────────────────────
if command -v wrk &>/dev/null; then
    log "4/5 wrk 压力测试..."

    if [ "$MODE" == "--full" ]; then
        DURATION="30s"
        CONNS="100"
        THREADS="4"
    else
        DURATION="10s"
        CONNS="20"
        THREADS="2"
    fi

    # 4a) Health 端点
    log "  4a) GET /health ($DURATION, ${CONNS}c/${THREADS}t)..."
    echo "$PAYLOAD_HEALTH" > /tmp/agent-prod-wrk-body.json
    wrk -t"$THREADS" -c"$CONNS" -d"$DURATION" --latency \
      "$HOST/health" 2>&1 | tee "$REPORT_DIR/wrk-health.txt"

    # 4b) Evaluate 端点
    log "  4b) POST /v1/agent/evaluate ($DURATION, ${CONNS}c/${THREADS}t)..."
    echo "$PAYLOAD_EVAL" > /tmp/agent-prod-wrk-eval.json
    wrk -t"$THREADS" -c"$CONNS" -d"$DURATION" --latency \
      -s <(cat <<'WRKSCRIPT'
wrk.method = "POST"
wrk.headers["Content-Type"] = "application/json"
wrk.body = [[{"agent":"hermes","session_id":"bench-","traffic_percentage":1,"decisions":[{"decision_id":"d1","model":"gpt-4","prompt_tokens":100,"completion_tokens":50,"tool_calls":[{"tool_id":"t1","tool_name":"search","arguments":{"q":"test"},"result_summary":"ok","success":true}]}],"current_metrics":{"latency_p95_ms":300,"success_rate":0.99,"expected_answer":"ok","final_response":"ok"},"human_approver":"bench"}]]
WRKSCRIPT
) "$HOST/v1/agent/evaluate" 2>&1 | tee "$REPORT_DIR/wrk-evaluate.txt"

    # 4c) Metrics 端点
    log "  4c) GET /metrics..."
    wrk -t"$THREADS" -c"$CONNS" -d"$DURATION" --latency \
      "$HOST/metrics" 2>&1 | tee "$REPORT_DIR/wrk-metrics.txt"

else
    warn "4/5 wrk not installed — skipping load test"
    warn "  Install: apt-get install wrk / brew install wrk"
fi

# ── 5) Benchmark 摘要 ────────────────────────────────────
log "5/5 Benchmark 摘要..."

HISTORY_FILE="$REPORT_DIR/history.jsonl"
SUMMARY=$(python3 -c "
import json, os, datetime

r = {
    'timestamp': datetime.datetime.utcnow().isoformat(),
    'host': '$HOST',
    'mode': '$MODE',
}

# Parse wrk results if available
for fname in ['wrk-health.txt', 'wrk-evaluate.txt', 'wrk-metrics.txt']:
    fpath = os.path.join('$REPORT_DIR', fname)
    if os.path.exists(fpath):
        with open(fpath) as f:
            content = f.read()
        for line in content.split('\n'):
            if 'Requests/sec' in line:
                r[f'{fname.replace(\".txt\",\"\")}_rps'] = line.strip().split()[-1]
            if 'Latency' in line and 'avg' in line.lower():
                r[f'{fname.replace(\".txt\",\"\")}_latency_avg'] = line.strip()
                break

print(json.dumps(r, indent=2))
with open('$HISTORY_FILE', 'a') as f:
    f.write(json.dumps(r) + '\n')
")
echo "$SUMMARY"

# ── 6) 最终 Metric 检查 ──────────────────────────────────
log "Pipeline metrics 快照:"
curl -sf "$HOST/metrics" 2>/dev/null | grep -E 'agent_prod_pipeline_total|agent_prod_gates_passed|agent_prod_rejections' || echo "  (no pipeline metrics yet — run evaluate first)"

echo ""
echo "报告目录: $REPORT_DIR"
echo "完成."
