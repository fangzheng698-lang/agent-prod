# agent-prod

[English](README.md) | [简体中文](README.zh-CN.md) | [Design](docs/DESIGN.md) | [MCP Integration](docs/MCP_INTEGRATION.md) | [Usage](docs/USAGE.md)

**Quality gates for production AI agents.** agent-prod wraps any agent run or
release in an 8-gate safety pipeline — permission, budget, trace integrity,
regression, gray release, audit, answer quality, and execution consistency.
Like tests for code, but for agent behavior.

```python
from agent_prod import trace

result = trace(
    agent="my-agent",
    session_id="session-001",
    current_metrics={"final_response": "Paris", "success_rate": 0.99},
)
print(result["status"])  # "production" | "rejected"
```

## Pipeline

```
Agent run ──▶ Gate0 ──▶ Gate1 ──▶ Gate2 ──▶ Gate3 ──▶ Gate4 ──▶ Gate5 ──▶ Gate6 ──▶ Gate7 ──▶ Approve?
              │         │          │          │          │          │          │          │
             risk     budget    trace     regress.   gray      audit     answer    exec.
             ACL      check     DAG       compare   rollout   policy    quality   consistency
```

| Gate | What it checks | When it blocks |
|---|---|---|
| **Gate0** Permission | Tool ACL, risky arg inspection, declared-tools enforcement | Undeclared or dangerous tool calls |
| **Gate1** Budget | Token & time budgets per agent type, circuit breaker | Budget exceeded or LLM endpoint degraded |
| **Gate2** Trace Integrity | LLM → tool DAG completeness, no orphan tool calls | Missing or unmapped LLM calls |
| **Gate3** Regression | Latency/success-rate/quality drift vs. evolving baseline | Significant performance or quality drop |
| **Gate4** Gray Release | Progressive rollout stages (1% → 10% → 50% → 100%) | Error-rate or latency spike per stage |
| **Gate5** Release Audit | Policy-as-code rules: prior gates, rollback plan, human approval | Critical policy violation |
| **Gate6** Answer Quality | Checklist evaluator (12 binary checks) or LLM-as-judge | Score below per-agent threshold |
| **Gate7** Execution Consistency | Plan-to-output alignment, goal fulfillment | Off-plan or hallucinated execution |

> **How this differs from eval frameworks:** Eval frameworks score a single
> dimension (answer correctness) in isolation. agent-prod closes the loop —
> permission → budget → trace → regression → release → audit → quality →
> consistency — and rejects the run at the **first** failure. This is the
> difference between "this answer scores 0.85" and "this agent run is not safe
> for production." [See design philosophy →](docs/DESIGN.md)

## Quick Start

```bash
# Install
pip install agent-prod

# Evaluate one trace — no server needed
python -c "
from agent_prod import trace
result = trace(
    agent='demo',
    session_id='demo-1',
    decisions=[{
        'decision_id': 'd1',
        'model': 'gpt-4',
        'prompt_tokens': 100,
        'completion_tokens': 50,
        'tool_calls': [
            {'tool_name': 'web_search', 'arguments': {'q': 'weather'}, 'success': True},
        ],
    }],
    current_metrics={'final_response': 'Sunny, 22°C', 'success_rate': 1.0},
    human_approver='demo',
)
print(f'Passed: {result[\"status\"]}')
"

# Or start the server
agent-prod configure
agent-prod serve
python examples/basic_trace.py
agent-prod stats
```

![agent-prod terminal demo](docs/assets/demo-terminal.svg)

## MCP Server — Use From Any Agent

agent-prod exposes all 8 gates as [MCP](https://modelcontextprotocol.io) tools.
Claude Desktop, Cursor, Cline — any MCP client can call quality-gate
evaluations directly.

```bash
pip install "agent-prod[mcp]"
agent-prod-mcp
```

```json
// claude_desktop_config.json
{ "mcpServers": { "agent-prod": { "command": "agent-prod-mcp" } } }
```

| MCP Tool | Purpose |
|---|---|
| `evaluate_trace` | Full Gate0–Gate7 pipeline for an agent trace |
| `check_tool_safety` | Single tool-call Gate0 preflight |
| `get_gate_stats` | Historical evaluation stats |
| `health_check` | Engine and repository health |

→ [Full MCP integration guide](docs/MCP_INTEGRATION.md)

## MCP Registry — Publish, Search, Install

```bash
# Publish your MCP server
agent-prod registry publish my-server \
    --command "uvx my-server" \
    --description "Search and index documentation" \
    --tags "search,docs"

# Search for servers
agent-prod registry search mcp

# List local registry
agent-prod registry list
```

→ [Registry source](src/agent_prod/registry/)

## Agent Observability — OpenTelemetry Spans

Wrap pipeline evaluations with OpenTelemetry spans. Each gate becomes a span
under an agent-run trace, exportable to any OTLP-compatible backend (Grafana,
Honeycomb, SigNoz).

```python
from agent_prod.observability.otel import AgentSpanExporter

exporter = AgentSpanExporter(endpoint="http://localhost:4317")
exporter.export_pipeline(improvement, agent_type="hermes")
# → Gate0–Gate7 spans exported to your observability backend
```

Zero hard dependency — works without opentelemetry installed. Activate with
`pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp`.

## A2A — Agent-to-Agent Delegation

Delegate tasks between agents via a lightweight protocol with capability
negotiation, partial-success semantics, and error chain attribution.

```python
from agent_prod.a2a import A2AAgent, A2ATask, A2ADelegator

class SearchAgent(A2AAgent):
    capabilities = ["web_search", "news"]

    def execute(self, task: A2ATask):
        results = search(task.input["q"])
        return {"results": results}

delegator = A2ADelegator()
delegator.register(SearchAgent())

task = A2ATask(name="search", input={"q": "weather"}, required_capabilities=["web_search"])
result = delegator.delegate(task)
```

Comes with a LangChain adapter (`create_langchain_tool`) to plug into existing
agent pipelines.

## Multi-Agent Trust Chain — Phase 5

When a parent agent delegates to a child, the child inherits a *subset* of the
parent's authority — never more. `TrustChainValidator` enforces this at Gate0:

```python
from agent_prod.gates.trust_chain import TrustChainValidator, TaskACL, TrustLevel

tc = TrustChainValidator()
tc.register_task(TaskACL(
    task_id="task-001",
    parent_agent="qclaw",
    child_agent="code-reviewer",
    trust_level=TrustLevel.RESTRICTED,
    allowed_tools={"Read", "Grep"},          # child can only call these
    allowed_domains={"finance"},             # child can only operate in this domain
    expires_at=datetime.now(UTC) + timedelta(hours=1),
))

# Gate0 now blocks any tool call from `child_agent=code-reviewer`
# outside the ACL — even if it's declared in declared_tools.
```

| Trust level | Behavior |
|---|---|
| `RESTRICTED` (default) | Child can only use tools in `allowed_tools` |
| `FULL` | Child inherits parent's full tool palette — no restriction |
| `SANDBOX` | Read-only / benign tools only; auto-expires |

Without a registered ACL, the validator stays out of the way — Gate0 falls
back to its standard declared-tools + auth-grant checks. So trust-chain
enforcement is opt-in per delegation relationship.

## Async Approval Workflow — Phase 3

Gate5 can emit a `pending_approval` state instead of binary pass/fail. The
pipeline halts, the improvement is persisted, and the pipeline resumes on
external decision — via HTTP callback, CLI, or webhook.

```bash
# Pipeline reaches Gate5 with only "Human approval" missing
# → status: pending_approval, improvement persisted

# Approve via HTTP
curl -X POST http://localhost:8080/v1/approvals/$APPROVAL_ID/decide \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "approver": "alice", "reason": "looks good"}'
# → runs Gate6, Gate7, promotes to PRODUCTION

# Or reject
curl -X POST http://localhost:8080/v1/approvals/$APPROVAL_ID/decide \
  -d '{"approved": false, "approver": "bob", "reason": "rollback plan missing"}'
# → status: rejected, fail_gate: gate5_approval
```

| Endpoint | Purpose |
|---|---|
| `GET /v1/approvals` | List approval records (filter by `agent`, `status`) |
| `GET /v1/approvals/{id}` | Fetch a single record |
| `POST /v1/approvals/{id}/decide` | Approve or reject; resumes pipeline |
| `POST /v1/approvals/{id}/approve` | Convenience: approve + resume |
| `POST /v1/approvals/{id}/reject` | Convenience: reject |

Approval records are idempotent — deciding twice raises `ValueError`. Records
auto-expire after a configurable TTL (default 24h).

### CLI

```bash
agent-prod approval list                       # list pending approvals
agent-prod approval list --agent qclaw         # filter by agent
agent-prod approval list --status pending      # filter by status
agent-prod approval approve <id> --approver alice --reason "LGTM"
agent-prod approval reject <id> --approver bob
```

### Server-restart rehydration

The in-memory `ApprovalQueue` is lost on server restart, but Improvement
records with `status=PENDING_APPROVAL` persist in the repository. On startup,
the server scans the repository for pending approvals and re-registers them in
the queue — so callbacks received after a restart still resume the pipeline.

## Domain Policy Engine — Phase 2

Industry-specific risk escalation at Gate0. The base tool risk is *only
escalated, never downgraded* when an agent operates in a regulated domain.

```yaml
# config.yaml
gates:
  domain_policy:
    enabled: true
    domains:
      finance:
        risk_overrides:
          web_search: elevated   # benign → elevated in finance context
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
# RiskLevel.ELEVATED  (was BENIGN in vanilla Gate0)

# Compliance claims are enforced: missing HIPAA claim in medical domain → block
cr = engine.validate_compliance_claims(
    tool="read_file", domain="medical",
    compliance_claims={},  # missing HIPAA
    agent_type="my-agent",
)
assert cr.violation  # ViolationType.MISSING_COMPLIANCE
```

## Observability — Prometheus + Grafana — Phase 4

```bash
# Spin up the full monitoring stack
docker compose --profile observability up -d
# → Prometheus on :9090, Grafana on :3001
```

Pre-built dashboard (`deploy/grafana/dashboards/agent_prod_overview.json`)
auto-provisions with panels for: pipeline outcomes, per-gate rejection rates,
gate1 circuit breaker state, domain-policy escalations, and approval queue
depth. The dashboard is provisioned automatically on first Grafana boot —
no manual import required.

Key Prometheus metrics exposed on `:8080/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `agent_prod_pipeline_total` | counter | status, agent |
| `agent_prod_gate_duration_ms` | histogram | — |
| `agent_prod_rejections_total` | counter | gate |
| `agent_prod_gate1_degraded` | gauge | — |
| `agent_prod_domain_escalations_total` | counter | domain, tool, agent |
| `agent_prod_domain_compliance_blocks_total` | counter | domain, violation_type |

## GatePlugin Interface — Extend With One Class

Every gate is a plug-in. Write your own gate in ~30 lines:

```python
from agent_prod.gates.interface import GatePlugin, register_gate
from agent_prod.gates.models import GateName, GateResult, Improvement

class MyCustomGate(GatePlugin):
    name = GateName("my_custom_gate")
    rollback_level = RollbackLevel.L1

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

The engine discovers gates through the `GatePlugin` ABC — no monkey-patching,
no framework fork. Add a gate, register it, and `from_yaml()` picks it up.
→ [Full interface design →](docs/DESIGN.md)

## Proof Points

| Signal | Evidence |
|---|---|
| 217 real agent sessions | Validated against Hermes traces |
| 4,345 tool calls | Exercised tool-risk and trace-integrity paths |
| 221 tests | CI passes without warnings |
| Dogfood report | [docs/DOGFOOD_REPORT.md](docs/DOGFOOD_REPORT.md) — self-evaluation with 70% pass rate |

## Why Not Just an Eval Framework?

| | Eval frameworks | agent-prod |
|---|---|---|
| **Scope** | Score one answer | Gate the whole run (8 dimensions) |
| **Flow** | Submit → score → report | Gate0 → Gate1 → … → reject early |
| **Persistence** | Stateless | Full state machine: candidate → production → rejected → rolled back |
| **Complexity** | One metric | Policy, audit trail, gray release, auto-rollback |
| **Integration** | Standalone | SDK + MCP server + config-as-code |

Eval frameworks answer *"how good is this output"*. agent-prod answers
*"is this agent run safe for production"*.

## Deployment

```bash
# One command — Postgres + agent-prod + MCP
docker compose up -d
```

See [docker-compose.yml](docker-compose.yml) and [.env.example](.env.example).

## Start Here

- [Design document](docs/DESIGN.md) — architecture decisions, GatePlugin ABC, and pipeline topology
- [MCP Integration guide](docs/MCP_INTEGRATION.md) — Claude Desktop, Cursor, Cline, Hermes setup
- [MCP Registry](src/agent_prod/registry/) — publish, search, and install MCP servers
- [A2A Protocol](src/agent_prod/a2a/) — agent-to-agent delegation
- [Observability](src/agent_prod/observability/otel.py) — OpenTelemetry spans for agent runs
- [Examples](examples/) — runnable traces and release scenarios
- [Usage guide](docs/USAGE.md) — CLI, configuration, Gate0–Gate7 details
- [Dogfood report](docs/DOGFOOD_REPORT.md) — we ate our own dog food
- [Calibration guide](docs/CALIBRATION.md) — tuning Gate5/Gate6 for your agents
- [Roadmap](ROADMAP.md) — production validation plan and next proof points

## License

MIT License. See [LICENSE](LICENSE).