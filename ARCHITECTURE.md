# agent-prod 架构文档

版本: v0.7.0-rc | 最后更新: 2026-06-25

## 一句话

agent-prod 是一个 **Agent CI/CD 质量管道**——把每次 agent trace 当成一次 build，跑七道门，从检测到归因到修复到基线演进，闭环判定"能不能上线"。

---

## 目录结构

```
agent-prod/
├── src/agent_prod/
│   ├── gates/                    # 七道门 + 引擎 + 配置 (核心)
│   │   ├── engine.py             # QualityGateEngine — 管道编排 + 闭环逻辑
│   │   ├── models.py             # Improvement, GateResult, GateName 枚举
│   │   ├── repository.py         # FileRepository (生产) / MemoryRepository (开发)
│   │   ├── config.yaml           # 所有阈值配置
│   │   ├── gate0_permission.py   # Gate0: 工具权限 + 参数审计
│   │   ├── gate1_execution.py    # Gate1: 执行资源 (schema + budget + 熔断)
│   │   ├── gate2_trace.py        # Gate2: trace 完整性 + OTel 可达性
│   │   ├── gate3_regression.py   # Gate3: 性能回归检测 + 归因触发
│   │   ├── gate4_gray.py         # Gate4: 灰度发布状态机 (1%→10%→50%→100%)
│   │   ├── gate5_audit.py        # Gate5: 人工审批 + policy tag 审计
│   │   ├── gate6_answer_quality.py # Gate6: 答案正确性评估
│   │   ├── attribution.py        # [v0.7] 归因引擎: metric回归→decision/tool_call根因
│   │   ├── error_classifier.py   # [v0.7] 错误分类器: fact/hallucination/omission/precision/format
│   │   ├── tool_risk.py          # 54 个工具的风险分类 (benign/elevated/dangerous)
│   │   ├── argument_inspection.py # 每个工具的威胁参数模式
│   │   ├── alerts.py             # 告警分发
│   │   ├── errors.py             # ErrorCode 枚举 + AppError
│   │   ├── config_schema.py      # Pydantic v2 schema 启动时校验 config.yaml
│   │   ├── thresholds.py         # 阈值热更新
│   │   ├── tool_executor.py      # Gate0 运行时工具拦截中间件
│   │   ├── auth_grants.py        # 高危工具授权记录
│   │   ├── interface.py          # GatePlugin ABC
│   │   └── metrics.py            # 指标抽象层
│   ├── gateway/
│   │   └── gateway.py            # QualityGateGateway — 外部 trace 入口
│   ├── server/
│   │   ├── app.py                # FastAPI + /v1/ /v2/ 端点 + 版本废弃
│   │   └── schemas.py            # Pydantic 响应模型
│   ├── trace/
│   │   ├── models.py             # AgentTrace, Decision, ToolCall 数据模型
│   │   └── adapters.py           # AgentTrace → Improvement 转换
│   ├── adaptivity/               # 自适应闭环 (loop orchestrator 等)
│   ├── agent/                    # LLM client / tools
│   ├── ingest/                   # Hermes 会话采集 + watchdog
│   ├── lifecycle/                # eval_loop, loop_state
│   ├── integration/              # hermes_evaluator 钩子
│   ├── cli.py                    # agent-prod serve / migrate / doctor
│   └── trace_client.py           # [v0.7] Python SDK — trace() / quick()
├── tests/
│   ├── test_gates_core.py        # Gate0/Gate3/Gate6/Repository/Config/Pipeline (18 tests)
│   ├── test_loop_state.py
│   ├── test_loop_orchestrator.py
│   ├── test_orch_governance.py    # 编排治理
│   └── test_benchmark_stress.py
├── scripts/
│   └── benchmark.sh              # [v0.7] wrk 压测脚本 (--quick / --full)
├── Dockerfile                    # 多阶段 Python 3.11-slim
├── docker-compose.yml            # 含 OTel Collector
├── otel-collector-config.yaml    # [v0.7] OTLP gRPC receiver
├── FINAL_ASSESSMENT.md           # 成熟度终评 (5.0 → 7.7)
└── setup.sh
```

---

## 核心数据流

```
外部 agent (Hermes / Claude Code / Codex)
       │
       │  agent 执行完毕，打包 trace
       ▼
POST /v1/agent/evaluate  ─────  JSON 请求体
       │
       ├─ { agent, session_id, decisions, current_metrics, baseline_metrics, ... }
       │
       ▼
app.py:_parse_agent_trace_from_dict()
       │
       │  ① 解析 decisions/tool_calls → AgentTrace 对象
       │  ② _parse_metrics() 将 expected_answer/final_response 注入 MetricsSnapshot.custom
       │
       ▼
gateway.evaluate_agent_trace(trace)
       │
       │  ③ adapter.to_improvement(trace) → Improvement
       │     └─ GenericAdapter: 映射 baseline/candidate_output, metadata
       │        └─ metadata: agent, declared_tools, decisions, auth_grant_id
       │
       ▼
engine.run_pipeline(improvement)
       │
       │  ④ [v0.7] 先存 CANDIDATE 快照 (防崩溃丢状态)
       │  ⑤ 遍历 7 门 pipeline
       │     Gate0 → Gate1 → Gate2 → Gate3 → Gate4 → Gate5 → Gate6
       │     │                                    │
       │     │  REJECT: 回滚 + 持久化              │
       │     │  → Gate3 触发归因引擎               │
       │     │  → Gate6 触发错误分类               │
       │     │  → [v0.7] 组装 auto_fix_prompt      │
       │     │                                    │
       │     PRODUCTION ← 7/7 passed              │
       │     → [v0.7] 基线自动演进                 │
       │     → 持久化成功状态                       │
       ▼
返回: { status, passed, gates: [{gate_name, passed, reason, details}] }
```

### 各 Gate 读 Improvement 的哪个字段

| Gate | 读取字段 | 说明 |
|------|---------|------|
| Gate0 | `metadata.decisions`, `metadata.declared_tools`, `metadata.auth_grant_id` | 工具调用 + 声明对比 |
| Gate1 | `actual_tokens`, `actual_time_ms`, `metadata.agent` | 预算 + schema |
| Gate2 | `trace_id`, `metadata.decisions` | DAG 校验 + OTel query |
| Gate3 | `candidate_output`, `baseline_output`, `metadata.agent` | 性能回归 |
| Gate4 | `traffic_percentage`, `metadata.gray_release_active` | 灰度阶梯 |
| Gate5 | `human_approver`, `policy_tags` | 审批 + 标签 |
| Gate6 | `candidate_output.final_response`, `candidate_output.expected_answer` | 答案正确性 |

### final_response 数据流 (坑位)

```
current_metrics.final_response
    → app._parse_metrics() → MetricsSnapshot.custom["final_response"]
    → adapters.map_metrics_to_baseline_candidate() → candidate_output["final_response"]
    → gate6.verify() → candidate = improvement.candidate_output.get("final_response", "")
```

如果客户端把 `final_response` 放在 `decisions[].tool_calls[].result_summary` 里，app.py 有自动提取回退。但最佳实践是放 `current_metrics` 里。

### declared_tools 数据流 (坑位)

```
请求体 declared_tools
    → AgentTrace.declared_tools
    → adapter.to_improvement() → improvement.metadata["declared_tools"]
    → gateway.evaluate_agent_trace() 再次注入 (双重保险)
    → gate0.verify() → improvement.metadata.get("declared_tools", [])
```

如果 adapter 不改 + gateway 不改，Gate0 只能看到空列表 → 所有工具调用被拒。

---

## 闭环管道 (v0.7)

v0.6 是开环：trace 入 → 判 → PASS/REJECT 出。
v0.7 是闭环：检测 → 归因 → 修复 → 演进。

```
                    ┌─ Gate3 REGRESSION ─────┐
                    │  → AttributionEngine    │
                    │    metric回归            │
                    │    → decision X          │
                    │    → tool_call Y 慢了Z   │
                    │    → fix_prompt          │
                    │                         │
trace → pipeline ───┤                         ├── REJECTED
                    │                         │   ├─ auto_fix_prompt (归因+分类拼接)
                    │                         │   └─ 持久化
                    │                         │
                    └─ Gate6 ANSWER_ERROR ────┘
                       → ErrorClassifier
                          fact/hallucination/omission/precision/format
                          → fix_suggestion

                    ┌─ Gate0-6 ALL PASS ─────┐
                    │  → _evolve_baseline()   │
                    │    PRODUCTION 的数据     │
                    │    → 覆盖为新 baseline    │
                    │    → 下次 Gate3 以此为基准│
                    └─────────────────────────┘
```

---

## 接入方式

### 方式 A: SDK (最简单)

```python
from agent_prod import trace

result = trace(
    agent="my-agent",
    session_id="ses_001",
    decisions=[{
        "decision_id": "d1",
        "model": "gpt-4",
        "tool_calls": [
            {"tool_id": "t1", "tool_name": "web_search", "arguments": {"q": "..."}, "success": True}
        ]
    }],
    current_metrics={"expected_answer": "巴黎是法国的", "final_response": "巴黎是法国的"}
)
print(result.status)  # "production" or "rejected"
```

SDK 在 `/root/experiment/agent-prod/src/agent_prod/trace_client.py`，纯 Python 标准库，零外部依赖。也提供 `quick()` 和 `evaluate_batch()`。

### 方式 B: curl / HTTP

```bash
curl -X POST http://agent-prod:8765/v2/agent/evaluate \
  -H 'Content-Type: application/json' -d @trace.json
```

### 方式 C: 运行时工具拦截 (Gate0 only)

```bash
POST /v1/tool/execute
{ "tool_name": "terminal", "arguments": {"command": "rm -rf /"}, "declared_tools": [...] }
→ Gate0 参数检测 → BLOCK
```

---

## 部署

```bash
# 快速启动
cd /root/experiment/agent-prod
python3 -m agent_prod.cli serve --port 8765

# Docker
docker-compose up -d   # 含 OTel Collector

# 单命令 (生产)
RATE_LIMIT_ENABLED=false agent-prod serve --port 8765
```

数据目录: `/var/lib/quality_gates/improvements.json` (可在 config.yaml 的 `storage.file_path` 改)

---

## 配置要点

所有阈值在 `src/agent_prod/gates/config.yaml`。关键配置项:

```yaml
gates:
  gate3:
    auto_evolve_baseline: true    # PRODUCTION → 自动更新基线
    dynamic_baseline: true        # 从历史 PRODUCTION 动态算基线
    baseline_window: 20           # 取最近 20 条
  gate6:
    evaluator: exact-match        # exact-match | llm-judge | semantic | pre-scored
    pass_threshold: 0.70          # score >= 0.7 → PASS
  auto_fix:
    enabled: true                 # REJECTED 时自动生成 fix_prompt
    max_retries: 3
    cooldown_minutes: 5
```

---

## 已知限制

1. **`search` 工具名不在风险注册表** — Gate0 的 `TOOL_RISK` 只有 `web_search`，用 `search` 会被当 unknown 工具拒绝。需要在客户端映射 `search` → `web_search`

2. **`exact-match` 评估器只做字面比较** — 标点、空格、近义词都不容忍。生产环境建议 `llm-judge` 或 `semantic`

3. **OTel Collector 只是可达性验证** — Gate2 的 `OtelCollectorClient` 发 gRPC health check，不做真 span ingest。需要在 Collector 端配导出器

4. **wrk 未预装** — benchmark.sh 依赖 wrk，需 `apt install wrk` 或换用 `ab`

5. **归因引擎依赖 baseline 有 `_decisions`** — 如果 baseline 没有 `_decisions` 字段，AttributionEngine 无法做 tool_call 级 diff，只能给 metric 级报告

6. **Gate4 状态机数据不跨服务实例共享** — `_gray_state` 是内存 dict，多实例部署时灰度进度不共享（需外移到 Redis）

---

## 测试

```bash
cd /root/experiment/agent-prod
python3 -m pytest tests/ -q
# 预期: 70 passed

# 单独跑新门测试
python3 -m pytest tests/test_gates_core.py -v
```

---

## 版本历史

| 版本 | 变化 |
|------|------|
| v0.5.0 | 7 门 pipeline 基础: Gate0-Gate6, MemoryRepository |
| v0.6.0 | 成熟度审计: FileRepository 并发锁、Pipeline 超时、config schema、Docker、错误码统一、/v1 废弃、/v2 别名、阈值热更新 |
| v0.7.0-rc | 闭环: 归因引擎、错误分类器、自动修复管道、基线自动演进、OTel Collector、Prometheus metrics、SDK、压测 |

---

## 相关文档

- `FINAL_ASSESSMENT.md` — 成熟度终评 (5.0 → 7.7, 20 项差距审计)
- `OPS_MANUAL.md` — 运维手册
- `ROADMAP.md` — 路线图
- `COMPARISON_ANALYSIS.md` — 与 OctoBus / agent-compose 对比
