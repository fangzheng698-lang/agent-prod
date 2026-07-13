# agent-prod — AI Agent 生产质量门禁

[English](README.md) | [简体中文](README.zh-CN.md) | [设计文档](docs/DESIGN.md) | [MCP 接入](docs/MCP_INTEGRATION.md)

Agent 上线前的 8 道质量门——权限、预算、轨迹完整性、回归检测、灰度、审计、答案质量、执行一致性。像测试代码一样测试 Agent 行为。

```python
from agent_prod import trace

result = trace(
    agent="my-agent",
    session_id="session-001",
    current_metrics={"final_response": "巴黎是法国的首都", "success_rate": 0.99},
)
print(result["status"])  # "production" | "rejected"
```

## 管线

```
Agent 运行 ──▶ Gate0 ──▶ Gate1 ──▶ Gate2 ──▶ Gate3 ──▶ Gate4 ──▶ Gate5 ──▶ Gate6 ──▶ Gate7 ──▶ 批准?
               │         │          │          │          │          │          │          │
             风险      预算      轨迹      回归      灰度      审计      答案      执行
             ACL      检查      DAG      比较      发布      策略      质量      一致性
```

| 道门 | 检查内容 | 拦截条件 |
|---|---|---|
| **Gate0** 权限 | 工具 ACL、参数检查、声明工具强制 | 未声明或高危工具调用 |
| **Gate1** 预算 | Token 和时间预算，熔断器 | 超预算或 LLM 端点降级 |
| **Gate2** 轨迹完整性 | LLM→工具 DAG 完整性 | 孤立工具调用或 LLM 缺失 |
| **Gate3** 回归 | 延迟/成功率/质量漂移 vs 演进基线 | 显著性能或质量下降 |
| **Gate4** 灰度 | 渐进式发布阶段（1%→10%→50%→100%） | 阶段内错误率或延迟飙升 |
| **Gate5** 审计 | 策略即代码：先验门、回滚预案、人肉审批 | 关键策略违规 |
| **Gate6** 答案质量 | Checklist 评估（12 项二值检查）或 LLM-as-judge | 低于 per-agent 阈值 |
| **Gate7** 执行一致性 | 计划与输出对齐、目标达成 | 偏离计划或幻觉执行 |

> **和 Eval 框架的区别：** Eval 框架在真空中对单一维度评分。agent-prod
> 形成闭环——权限→预算→轨迹→回归→发布→审计→质量→一致性——在**第一道**
> 失败的门即拒绝本次运行。这是"这个回答 0.85 分"和"这次 Agent 运行不安全"
> 的区别。[查看设计哲学 →](docs/DESIGN.md)

## 5 分钟快速开始

```bash
# 安装
pip install agent-prod

# 一行代码评估一条 trace（无需启动服务）
python -c "
from agent_prod import trace
result = trace(
    agent='demo', session_id='demo-1',
    decisions=[{'decision_id': 'd1', 'model': 'gpt-4',
                'tool_calls': [{'tool_name': 'web_search', 'arguments': {'q': '天气'}, 'success': True}]}],
    current_metrics={'final_response': '晴天 22°C', 'success_rate': 1.0},
    human_approver='demo',
)
print(f'通过: {result[\"status\"]}')  # production
"

# 或启动服务
agent-prod serve
python examples/basic_trace.py
agent-prod stats
```

## MCP Server

所有 8 道门暴露为 MCP 工具，任何 MCP 客户端（Claude Desktop、Cursor、Cline）
可直接调用。

```bash
pip install "agent-prod[mcp]"
agent-prod-mcp
```

| MCP 工具 | 用途 |
|---|---|
| `evaluate_trace` | 完整 Gate0–Gate7 管线评估 |
| `check_tool_safety` | 单次工具调用的 Gate0 预检 |
| `get_gate_stats` | 历史评估统计 |
| `health_check` | 引擎健康检查 |

→ [完整 MCP 接入指南](docs/MCP_INTEGRATION.md)

## MCP Registry — 发布、搜索、安装

```bash
# 发布你的 MCP 服务器
agent-prod registry publish my-server \
    --command "uvx my-server" \
    --description "搜索和索引文档" \
    --tags "search,docs"

# 搜索服务器
agent-prod registry search mcp

# 列出本地注册表
agent-prod registry list
```

→ [Registry 源码](src/agent_prod/registry/)

## Agent Observability — OpenTelemetry Span

将管线评估包装为 OpenTelemetry span，每条门是一段 span，可导出到任何
OTLP 兼容后端（Grafana、Honeycomb、SigNoz）。

```python
from agent_prod.observability.otel import AgentSpanExporter

exporter = AgentSpanExporter(endpoint="http://localhost:4317")
exporter.export_pipeline(improvement, agent_type="hermes")
# → Gate0–Gate7 spans 导出到可观测性后端
```

无硬依赖，不安装 opentelemetry 时自动降级为 no-op。
`pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp` 激活。

## A2A — Agent 间任务委托

通过轻量协议在 Agent 之间委托任务，支持能力协商、部分成功语义和错误链归因。

```python
from agent_prod.a2a import A2AAgent, A2ATask, A2ADelegator

class SearchAgent(A2AAgent):
    capabilities = ["web_search", "news"]

    def execute(self, task: A2ATask):
        results = search(task.input["q"])
        return {"results": results}

delegator = A2ADelegator()
delegator.register(SearchAgent())

task = A2ATask(name="search", input={"q": "天气"}, required_capabilities=["web_search"])
result = delegator.delegate(task)
```

附带 LangChain 适配器（`create_langchain_tool`），可接入现有 Agent 管线。

## 多 Agent 信任链（Phase 5）

父 Agent 委托子 Agent 时，子 Agent 继承父 Agent 权限的*子集*——只少不多。
`TrustChainValidator` 在 Gate0 中实施这个约束：

```python
from agent_prod.gates.trust_chain import TrustChainValidator, TaskACL, TrustLevel

tc = TrustChainValidator()
tc.register_task(TaskACL(
    task_id="task-001",
    parent_agent="qclaw",
    child_agent="code-reviewer",
    trust_level=TrustLevel.RESTRICTED,
    allowed_tools={"Read", "Grep"},           # 子 Agent 只能调用这些工具
    allowed_domains={"finance"},              # 只能在这个行业域内操作
    expires_at=datetime.now(UTC) + timedelta(hours=1),
))

# Gate0 现在会拦截子 Agent 在 ACL 之外的任何工具调用——即使它在 declared_tools 中
```

| 信任等级 | 行为 |
|---|---|
| `RESTRICTED`（默认）| 子 Agent 只能用 `allowed_tools` 中的工具 |
| `FULL` | 继承父 Agent 全部工具——无限制 |
| `SANDBOX` | 只能调用只读/benign 工具；自动过期 |

没有注册 ACL 时，验证器不干预——Gate0 退回到标准声明的工具 + 授权检查。
所以信任链是按委托关系 opt-in 的。

## 异步审批流（Phase 3）

Gate5 可以发射 `pending_approval` 状态，而不是二元的通过/拒绝。管线暂停，
改进项持久化，等待外部决策通过 HTTP 回调、CLI 或 webhook 恢复。

```bash
# 管线到达 Gate5，唯一缺失的是"人工审批"
# → status: pending_approval，改进项已持久化

# 通过 HTTP 审批
curl -X POST http://localhost:8080/v1/approvals/$APPROVAL_ID/decide \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "approver": "alice", "reason": "没问题"}'
# → 运行 Gate6、Gate7，晋升为 PRODUCTION

# 拒绝
curl -X POST http://localhost:8080/v1/approvals/$APPROVAL_ID/decide \
  -d '{"approved": false, "approver": "bob", "reason": "回滚预案不完整"}'
# → status: rejected，fail_gate: gate5_approval
```

| 端点 | 用途 |
|---|---|
| `GET /v1/approvals` | 列出审批记录（可按 `agent`、`status` 过滤）|
| `GET /v1/approvals/{id}` | 查询单条记录 |
| `POST /v1/approvals/{id}/decide` | 批准或拒绝；自动恢复管线 |
| `POST /v1/approvals/{id}/approve` | 便捷：批准 + 恢复 |
| `POST /v1/approvals/{id}/reject` | 便捷：拒绝 |

审批记录是幂等的——重复决策会抛出 `ValueError`。记录在可配置的 TTL 后自动过期（默认 24 小时）。

## 行业域策略引擎（Phase 2）

在 Gate0 实现行业维度的风险升级。基础工具风险在受监管行业中**只能升不能降**。

```yaml
# config.yaml
gates:
  domain_policy:
    enabled: true
    domains:
      finance:
        risk_overrides:
          web_search: elevated   # benign → elevated（金融行业上下文）
        required_compliance: [sox, pci_dss]
      medical:
        risk_overrides:
          read_file: elevated
        required_compliance: [hipaa]
```

```python
from agent_prod.gates.domain_policy import DomainPolicyEngine

engine = DomainPolicyEngine(config)
result = engine.get_effective_risk("web_search", "my-agent", domain="finance")
# RiskLevel.ELEVATED（原本是 BENIGN）

# 合规声明检查：医疗域缺少 HIPAA 声明 → 阻断
cr = engine.validate_compliance_claims(
    tool="read_file", domain="medical",
    compliance_claims={},  # missing HIPAA
    agent_type="my-agent",
)
assert cr.violation  # ViolationType.MISSING_COMPLIANCE
```

## 可观测性 — Prometheus + Grafana（Phase 4）

```bash
# 启动完整监控栈
docker compose --profile observability up -d
# → Prometheus :9090, Grafana :3001
```

预置仪表盘 (`deploy/grafana/dashboards/agent_prod_overview.json`) 自动配
置了以下面板：管线结果、各门拒绝率、Gate1 熔断器状态、域策略升级数和审批
队列深度。首次启动 Grafana 时自动配置，无需手动导入。

`:8080/metrics` 暴露的关键 Prometheus 指标：

| 指标 | 类型 | 标签 |
|---|---|---|
| `agent_prod_pipeline_total` | counter | status, agent |
| `agent_prod_gate_duration_ms` | histogram | — |
| `agent_prod_rejections_total` | counter | gate |
| `agent_prod_gate1_degraded` | gauge | — |
| `agent_prod_domain_escalations_total` | counter | domain, tool, agent |
| `agent_prod_domain_compliance_blocks_total` | counter | domain, violation_type |

## GatePlugin 接口 — 继承一个类即可扩展

每道门都是插件。~30 行代码写自己的门：

```python
from agent_prod.gates.interface import GatePlugin, register_gate
from agent_prod.gates.models import GateName, GateResult, Improvement

class MyCustomGate(GatePlugin):
    name = GateName("my_custom_gate")

    def verify(self, improvement: Improvement) -> GateResult:
        if improvement.candidate_output.get("my_field", 0) >= 90:
            return GateResult(gate_name=self.name, passed=True, reason="OK")
        return GateResult(gate_name=self.name, passed=False, reason="my_field < 90")

    def rollback(self, improvement: Improvement) -> None:
        pass

    @classmethod
    def from_config(cls, config, name):
        return cls()

register_gate(GateName("my_custom_gate"), MyCustomGate)
```

引擎通过 `GatePlugin` ABC 发现所有门——无需猴子补丁，无需 Fork 框架。
→ [完整接口设计 →](docs/DESIGN.md)

## 和 Eval 框架有什么不同

| | Eval 框架 | agent-prod |
|---|---|---|
| **范围** | 给一个回答打分 | 门禁整个运行过程（8 个维度） |
| **流程** | 提交 → 评分 → 报告 | Gate0 → Gate1 → … → 早拒绝 |
| **状态** | 无状态 | 完整状态机：candidate → production → rejected → rolled back |
| **复杂度** | 一个指标 | 策略、审计追踪、灰度、自动回滚 |
| **集成** | 独立使用 | SDK + MCP 服务 + 配置即代码 |

Eval 框架回答"这个输出有多好"。agent-prod 回答"这次 Agent 运行是否安全"。

## 部署

```bash
docker compose up -d  # Postgres + agent-prod + MCP
```

## 证明

| 信号 | 证据 |
|---|---|
| 217 条真实会话 | 在 Hermes traces 上验证通过 |
| 4,345 次工具调用 | 全路径工具风险 + 轨迹完整性 |
| 219 个测试 | CI 全绿 |
| 自评估报告 | [docs/DOGFOOD_REPORT.md](docs/DOGFOOD_REPORT.md) — 70% 通过率 |

## 从这里开始

- [设计文档](docs/DESIGN.md) — 架构决策、GatePlugin ABC、管线拓扑
- [MCP 接入指南](docs/MCP_INTEGRATION.md) — Claude Desktop、Cursor、Cline、Hermes 配置
- [MCP Registry](src/agent_prod/registry/) — 发布、搜索、安装 MCP 服务器
- [A2A 协议](src/agent_prod/a2a/) — Agent 间任务委托
- [可观测性](src/agent_prod/observability/otel.py) — Agent 运行 OpenTelemetry Span
- [示例](examples/) — 可运行的 trace 和发布场景
- [使用指南](docs/USAGE.md) — CLI、配置、Gate0–Gate7 详解
- [自评估报告](docs/DOGFOOD_REPORT.md) — 我们吃自己的狗粮
- [校准指南](docs/CALIBRATION.md) — 为你的 Agent 调整 Gate5/Gate6 阈值
- [路线图](ROADMAP.md) — 生产验证计划和下一步

## License

MIT License. See [LICENSE](LICENSE).