# agent-prod — Enterprise AI Agent Framework v0.2.0

**Production-grade agent runtime skeleton with plug-in quality gates, causal attribution, and data flywheel.**

```
agent_prod/
├── agent/          Core runtime — AgentRuntime, LLMClient, ToolRegistry, Budget
├── gates/          Quality gates plug-in system — GatePlugin ABC + 5 built-in gates
│   ├── interface.py    ← THE STANDARD: subclass GatePlugin to add a gate
│   ├── gate1_execution.py    Structured output contract validation
│   ├── gate2_trace.py        LLM↔tool trace integrity
│   ├── gate3_regression.py   Output quality monitoring
│   ├── gate4_gray.py         Gradual traffic ramp
│   └── gate5_audit.py        Policy-as-code release audit
├── gateway/        Bridge: AgentRuntime output → QualityGateEngine pipeline
├── server/         FastAPI REST API (OpenAI-compatible /v1/chat/completions)
├── observability/  Embedded Prometheus metrics + structured execution logging
├── adaptivity/     Causal attribution (Granger + counterfactual) + data flywheel
├── lifecycle/      Session state machine + cross-session memory
├── testing/        Benchmark, replay, profiling, stress testing
└── ingest/         Real session ingestion pipelines
```

## Quickstart

```bash
pip install agent-prod
agent-prod init          # interactive setup wizard
agent-prod serve         # start the server
```

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

Response includes per-gate results:

```json
{
  "id": "ses_abc123",
  "quality_gate": {
    "status": "production",
    "passed": true,
    "gates": [
      {"gate": "gate1_execution", "passed": true, "reason": "Output matches ExecutionOutput contract"},
      {"gate": "gate2_trace_integrity", "passed": true, "reason": "Trace DAG verified"},
      {"gate": "gate3_regression", "passed": true, "reason": "No regression detected"},
      {"gate": "gate4_gray_release", "passed": true, "reason": "Gray release OK"},
      {"gate": "gate5_release_audit", "passed": true, "reason": "All policies passed"}
    ]
  }
}
```

## Architecture

### Gate Plugin Standard (the key differentiator)

Every gate implements the `GatePlugin` ABC:

```python
class GatePlugin(ABC):
    name: GateName

    @abstractmethod
    def verify(self, improvement: Improvement) -> GateResult: ...
    @abstractmethod
    def rollback(self, improvement: Improvement) -> None: ...
    @classmethod
    @abstractmethod
    def from_config(cls, config: dict, name: GateName) -> "GatePlugin": ...
```

To add a custom gate, subclass `GatePlugin`, call `register_gate()` on import, and add it to the pipeline order in config. No engine changes needed.

### Configuration Cascade

```
config.yaml  >  .env  >  environment variables
     ↑
   quality_gates:
     gate1: {threshold: 0.95}
     gate3: {prompt_diff: 0.3, content_diff: 0.5}
     gate4: {traffic_steps: [1,10,50,100]}
     ...
```

## Accumulate Real Data

```bash
# Ingest Hermes session history
python -m agent_prod.ingest.hermes_sessions --recent 50

# Run causal attribution on ingested data
python -m agent_prod.adaptivity.causal_attributor data/execution_log.jsonl
```

## Extras

```bash
pip install "agent-prod[all]"        # everything (Postgres, Prometheus, Jaeger, Unleash)
pip install "agent-prod[postgres]"   # just Postgres persistence
```

Production mode:

```bash
export QUALITY_GATES_MODE=production
export DATABASE_URL=postgresql+asyncpg://localhost/agent_prod
agent-prod serve
```

## Version

0.2.0 — Skeleton release. Phase 1-11 modules refactored into layered package structure with GatePlugin plug-in interface as public API contract.
