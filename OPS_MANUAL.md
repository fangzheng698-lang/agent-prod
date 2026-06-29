# agent-prod 操作手册

版本 0.2.0 | 零依赖嵌入式 | 任何 agent 可接入

---

## 1. 是什么

agent-prod 是一套**agent质量门禁基础设施**。任何agent执行完毕后，将trace提交到 `/v1/agent/evaluate`，经过5道质量门检查后返回 `pass/gray/reject` 决策。

```
agent执行 → POST /v1/agent/evaluate → gate1(schema) → gate2(trace) → gate3(回归) → gate4(灰度) → gate5(审计) → production/rejected
```

不绑任何框架。不要求agent改代码。旁路接入。

---

## 2. 一分钟启动

```bash
cd /root/experiment/agent-prod

# 启动服务（嵌入式模式，零外部依赖）
AGENT_PROD_URL=http://localhost:8765 python3 -m agent_prod serve --port 8765

# 验证
curl http://localhost:8765/health
```

---

## 3. CLI 命令

```
agent-prod serve        启动服务器
agent-prod watch        启动Hermes会话旁路监控（自动提交session到门禁）
agent-prod show thresholds  [--agent hermes]   查看各agent门禁阈值
agent-prod set threshold gate3 hermes regress_pct 0.93   设置阈值
agent-prod doctor       健康检查
```

---

## 4. API 端点

### 4.1 核心：`POST /v1/agent/evaluate`

任何agent执行完毕，打包trace提交此端点即可。

**请求体**（最小字段）：

```json
{
  "agent": "hermes",
  "session_id": "my-session-001",
  "output": {"final_response": "修复了登录超时bug"},
  "decisions": [
    {
      "decision_id": "turn-1",
      "model": "deepseek-v4-flash",
      "prompt_tokens": 1200,
      "completion_tokens": 450,
      "tool_calls": [
        {"tool_id": "t1", "tool_name": "read_file", "arguments": {"path": "src/auth.py"}, "success": true}
      ]
    }
  ],
  "current_metrics": {
    "latency_p95_ms": 3200,
    "success_rate": 0.98,
    "token_efficiency": 0.87
  },
  "baseline_metrics": {
    "latency_p95_ms": 4500,
    "success_rate": 0.94,
    "token_efficiency": 0.72
  },
  "policy_tags": ["production"],
  "human_approver": "alice"
}
```

**响应**：

```json
{
  "agent": "hermes",
  "session_id": "my-session-001",
  "status": "production",
  "passed": true,
  "gates": [
    {"gate": "gate1_execution", "passed": true, "reason": "All checks passed"},
    {"gate": "gate2_trace_integrity", "passed": true, "reason": "Trace integrity verified"},
    {"gate": "gate3_regression", "passed": true, "reason": "No critical regressions"},
    {"gate": "gate4_gray_release", "passed": true, "reason": "All gray stages passed"},
    {"gate": "gate5_release_audit", "passed": true, "reason": "Release audit passed"}
  ],
  "total_duration_ms": 4.2
}
```

**3种决策**：
- `production` — 全部通过，自动上线
- `gray` — 部分通过，进入灰度阶梯继续观察
- `rejected` — 一道门失败，阻断发布



### 4.2 其他端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/v1/agent/list` | GET | 支持的agent类型列表 |
| `/v1/agent/thresholds` | GET | 各agent阈值配置 |
| `/v1/agent/evaluate` | POST | **核心：提交trace评估** |
| `/evaluate/dry-run` | POST | 同上，不写库 |

---

## 5. 各种Agent接入方式

### 5.1 Hermes — 完全自动（两种机制，零代码改动）

**方式A：SessionDB Hook**（本次session同步触发）

```python
# 在Hermes的cli.py中注册一次：
from agent_prod.integration.hermes_evaluator import hermes_evaluator_hook
db.register_session_end_hook(hermes_evaluator_hook)
# 此后每次session结束时自动读取state.db → 构建trace → POST门禁
```

**方式B：SessionWatchdog**（旁路轮询，无需改Hermes）

```bash
# 启动旁路监控进程：
agent-prod watch --sessions-dir ~/.hermes/sessions --url http://localhost:8765
# 每1秒扫描 ~/.hermes/sessions/ 下的 session_*.json
# 文件出现/更新 → 自动解析 → POST门禁
```

### 5.2 Claude Code

```bash
# 方式1：一行包装
claude -p "$PROMPT" --json > /tmp/trace.json
agent-prod eval --agent claude-code --file /tmp/trace.json

# 方式2：直接curl
curl -X POST http://localhost:8765/v1/agent/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"agent":"claude-code","session_id":"cc-001","output":{...},"decisions":[...]}'
```

### 5.3 OpenAI Codex

```bash
codex exec "$PROMPT" --output-format json > /tmp/trace.json
curl -X POST http://localhost:8765/v1/agent/evaluate \
  -H 'Content-Type: application/json' \
  -d @/tmp/trace.json
```

### 5.4 OpenCode

```bash
opencode run "$PROMPT" --trace > /tmp/trace.json
curl -X POST http://localhost:8765/v1/agent/evaluate \
  -H 'Content-Type: application/json' \
  -d "$(cat /tmp/trace.json | jq '{agent:"opencode",session_id:.id,output:...,decisions:...}')"
```

### 5.5 任何自研Agent

只要能把执行结果打包成 `AgentTrace` 格式的JSON，POST过来即可。未注册的agent类型自动fallback到 `GenericAdapter` 默认阈值。

最小字段：`agent` + `session_id` + `output` + `decisions`。其他字段都有默认值。

---

## 6. 5道门详解

| 门 | 名称 | 检查内容 | 失败含义 |
|----|------|----------|----------|
| gate1 | Execution Schema | LLM实时检查输出是否符合合约 | 输出格式/内容不满足要求 |
| gate2 | Trace Integrity | 每个tool_call都有LLM父决策，无孤儿调用 | agent执行链路断裂 |
| gate3 | Regression Detection | 对比baseline：延迟/成功率/token效率 | 性能退化超阈值 |
| gate4 | Gray Release | 1%→10%→50%→100%四阶灰度，每阶观察N个cycle | 灰度阶梯异常 |
| gate5 | Release Audit | 策略合规 + human approver签名 | 审批链不完整 |

---

## 7. Per-Agent阈值

每个agent类型可以独立设置gate3和gate4的阈值。配置文件：`src/agent_prod/gates/config.yaml`

```yaml
gates:
  gate3:
    regress_pct: 0.95        # 全局默认
    per_agent:
      hermes:       { regress_pct: 0.93 }   # 对话agent，容忍度最高
      claude-code:  { regress_pct: 0.97 }   # 代码agent，要求最严格
      codex:        { regress_pct: 0.95 }
      opencode:     { regress_pct: 0.95 }
```

**CLI管理**：

```bash
# 查看阈值
agent-prod show thresholds --agent hermes

# 修改阈值（即时生效）
agent-prod set threshold gate3 hermes regress_pct 0.94
```

**设计逻辑**：同一个task，同一个success_rate=0.93：
- Claude Code → **REJECTED**（代码agent不允许5%+波动）
- Hermes → **PRODUCTION**（对话agent容忍度更高）

---

## 8. 闭环能力（LoopOrchestrator）

`LoopOrchestrator` 提供完整闭环 — 不依赖外部服务即可运行：

```python
from agent_prod.adaptivity.loop_orchestrator import LoopOrchestrator

orch = LoopOrchestrator()
result = orch.run_cycle_sync(prompt="optimize query", turns=[...])
print(result.summary)  # 4-phase + 11-state + benchmark + governance
```

**11状态机路径**：
```
candidate → executing → executed → attributing → attributed → optimizing → optimized → verifying → verified → releasing → completed
                                                                              ↳ rejected / rolled_back / error
```

**产出物**：
- `data/execution_log.jsonl` — 结构化执行日志
- `data/benchmarks/v*.json` — 版本化基准快照
- `data/replays/` — 可回放执行录制
- governance面板 — release/rollback/candidate/gray状态追踪

---

## 9. 目录结构

```
agent-prod/
├── src/agent_prod/
│   ├── gates/           # 5道门 + 门禁引擎 + per-agent阈值解析
│   ├── gateway/         # AgentTrace → Improvement 转换桥
│   ├── server/          # FastAPI REST API
│   ├── trace/           # AgentTrace统一数据模型 + Adapter注册表
│   ├── adaptivity/      # LoopOrchestrator + 因果归因 + 数据飞轮
│   ├── testing/         # Benchmark | Replay | Optimizer | Governance | GateStress
│   ├── lifecycle/       # 11-state状态机
│   ├── ingest/          # Hermes会话自动提取（watchdog + hermes_evaluator）
│   ├── integration/     # Hermes Hook集成
│   └── cli.py           # CLI入口
├── tests/               # 52个测试
├── pyproject.toml
└── README.md
```

---

## 10. 测试

```bash
cd /root/experiment/agent-prod
python3 -m pytest tests/ -v    # 52 tests, 全部通过
```

---

## 附录：快速验证脚本

```bash
# 1. 启动服务
AGENT_PROD_URL=http://localhost:8765 python3 -m agent_prod serve --port 8765 &

# 2. 提交评估
curl -s -X POST http://localhost:8765/v1/agent/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "agent": "hermes",
    "session_id": "demo-001",
    "output": {"final_response": "Fixed bug"},
    "decisions": [{"decision_id": "d1", "model": "gpt-4", "prompt_tokens": 100, "completion_tokens": 50,
      "tool_calls": [{"tool_id": "t1", "tool_name": "read_file", "success": true}]}],
    "current_metrics": {"latency_p95_ms": 1200, "success_rate": 0.99},
    "baseline_metrics": {"latency_p95_ms": 1800, "success_rate": 0.95},
    "policy_tags": ["production"],
    "human_approver": "alice"
  }' | python3 -m json.tool

# 3. 查看阈值
agent-prod show thresholds --agent hermes
```
