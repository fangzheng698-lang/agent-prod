# agent-prod 通用 Agent 质量门禁 — 缺口补全报告

## 补全概要

上次对话列出 7 个缺口，其中 3 个已在上次会话实现（AgentTrace 数据模型、TraceAdapter 接口、POST /v1/agent/evaluate 端点），第 5 个（trace→Improvement 映射）随 adapter 一起实现。

**本会话补全剩余 3 个真实缺口：**

| # | 缺口 | 状态 | 验证方式 |
|---|------|------|----------|
| A | 按 agent 类型分组阈值 | ✅ 已实现 | curl 验证：hermes(f1=0.90) PASS vs generic(f1=0.90) REJECT |
| B | 告警推送 | ✅ 已实现 | 单元测试 13 项通过，9 个 curl 端点验证 |
| D | 真实端点验证 | ✅ 已完成 | curl 打 POST /v1/agent/evaluate，6 个场景通过 |
| C | 实施文档 | ✅ 本文档 | — |

---

## Gap A: Per-Agent Type Thresholds

### 设计

不同 agent 有不同性能基线。Hermes 工具链复杂，允许更大容差；Claude Code 要求更严。

```
config.yaml:

gates:
  gate3:
    regress_pct: 0.95              # 全局默认
    per_agent:
      hermes:
        regress_pct: 0.93          # Hermes 只要求 93%
      claude-code:
        regress_pct: 0.97          # Claude Code 要求 97%
```

### 实现架构

```
config.yaml
    ↓ load_config()
engine._raw_config (dict)
    ↓ gate3.__init__(raw_config=...)
Gate3Regression._raw_config
    ↓ verify() → _resolve_config(improvement)
Gate3Config.resolve_for_agent("hermes", config) → Gate3Config(regress_pct=0.93)
    ↓ used in all verification methods
```

**新增文件:** `gates/thresholds.py` — `resolve_agent_thresholds(gate_name, agent_type, config)`  
**修改文件:** `gate3_regression.py`, `gate4_gray.py`, `engine.py`, `gateway.py`, `config.yaml`

### 实时验证 (curl)

```bash
# 场景1: Hermes agent, f1_score=0.90
# Hermes 阈值: 0.95 * 0.93 = 0.8835, 0.90 > 0.8835 → PASS
curl POST /v1/agent/evaluate -d '{"agent":"hermes",...,"current_metrics":{"custom":{"f1_score":0.90}},"baseline_metrics":{"custom":{"f1_score":0.95}}}'
→ {"status":"production","passed":true}

# 场景2: 相同数据，generic agent（无 per-agent override）
# 全局阈值: 0.95 * 0.95 = 0.9025, 0.90 < 0.9025 → REJECT
curl POST /v1/agent/evaluate -d '{"agent":"generic",...,...}'
→ {"status":"rejected","passed":false,"fail_reason":"1 critical regression(s): f1_score"}
```

---

## Gap B: Alert Push（告警推送）

### 设计

门禁拒绝时推送到 Discord / Telegram / 通用 webhook。可插拔后端，失败静默降级，不阻塞门禁管线。

```
QualityGateEngine.run_pipeline()
    ↓ gate fails → REJECTED
    ↓ _dispatch_alert(improvement)
    ↓ AlertDispatcher.send(payload)
    ↓ DiscordAlert.send()  ─┬─ 并行投递到所有后端
    ↓ TelegramAlert.send() ─┤
    ↓ WebhookAlert.send()  ─┘
```

**新增文件:** `gates/alerts.py` — `AlertPayload`, `AlertDispatcher`, `DiscordAlert`, `TelegramAlert`, `WebhookAlert`, `create_dispatcher_from_config`

### 配置

```yaml
# config.yaml
alerts:
  enabled: true
  discord:
    webhook_url: "https://discord.com/api/webhooks/..."
  telegram:
    bot_token: "123:abc"
    chat_id: "-1001234567890"
  webhook:
    url: "https://hooks.example.com/alerts"
    headers:
      Authorization: "Bearer token123"
```

### 告警文案

- **summary_text()**: 单行摘要 `[REJECTED] hermes/ses_abc: gate3_regression — f1_score degraded -5.2%`
- **to_markdown()**: 完整表格，含 5 道门逐个结果
- **Webhook JSON**: 结构化 `{"event":"gate_failure","agent_type":...,"failed_gate":...}`

### 验证

- Discord/Telegram/Webhook 不可达 → 返回 False，引擎继续
- BrokenBackend 抛 RuntimeError → 捕获、日志警告、管线不中断
- `Engine.from_yaml()` 自动从 config 创建 dispatcher (3 backends)

---

## Gap D: 实时端点验证

### 测试矩阵

| # | 场景 | 请求 | 结果 |
|---|------|------|------|
| 1 | GET /health | — | `{"status":"ok","quality_gates":true}` |
| 2 | GET /v1/agent/types | — | `["claude-code","codex","generic","hermes","opencode"]` |
| 3 | Hermes, 正常数据 | 5道门全部通过 | status=production, passed=true |
| 4 | Token 超预算 | gate1 失败 | status=rejected, fail_reason=Truokens over budget |
| 5 | Unknown agent | GenericAdapter 降级 | status=production (graceful fallback) |
| 6 | Hermes f1=0.90 | hermes 阈值放行 | status=production (per-agent) |
| 7 | Generic f1=0.90 | 全局阈值拦截 | status=rejected (global default) |

**全部通过。**

---

## 项目当前状态

### 测试覆盖

```
test_phase1_real.py           27 tests ✅
test_per_agent_thresholds.py  15 tests ✅
test_alerts.py                13 tests ✅
─────────────────────────────────────
Total                         55 tests ✅
```

### 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `gates/config.yaml` | 修改 | per_agent 阈值分组 + alerts 配置段 |
| `gates/thresholds.py` | 新增 | 阈值解析函数 |
| `gates/alerts.py` | 新增 | 告警推送系统 (AlertPayload/Dispatcher/3 backends) |
| `gates/gate3_regression.py` | 修改 | _resolve_config + cfg 参数穿透 |
| `gates/gate4_gray.py` | 修改 | _resolve_config + cfg 参数穿透 |
| `gates/engine.py` | 修改 | raw_config 传递 + AlertDispatcher 集成 |
| `gateway/gateway.py` | 修改 | memory() 加载 default config.yaml |
| `tests/test_per_agent_thresholds.py` | 新增 | 15 tests |
| `tests/test_alerts.py` | 新增 | 13 tests |

### 架构一览

```
外部 Agent (Hermes/Claude Code/Codex/OpenCode)
    │
    │ AgentTrace JSON
    ▼
POST /v1/agent/evaluate
    │
    ▼
AdapterRegistry.get(agent_type) → GenericAdapter/HermesAdapter/...
    │
    │ AgentTrace → Improvement
    ▼
QualityGateEngine.run_pipeline()
    │
    ├── Gate1 (Execution: budget, output schema)
    ├── Gate2 (Trace Integrity: DAG, orphans)
    ├── Gate3 (Regression: per-agent thresholds ✨)
    ├── Gate4 (Gray Release: per-agent thresholds ✨)
    ├── Gate5 (Audit: policy, approvals)
    │
    ├── On REJECTED → AlertDispatcher.send() ✨
    │
    ▼
EvaluateResult {status: "production"|"rejected"|...}
```
