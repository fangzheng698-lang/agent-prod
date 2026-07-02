# agent-prod Design

> If Kubernetes' moat is the CRI/CNI/CSI interface design, agent-prod's moat is
> the quality-gate interface: Gate ABCs, Gate0–Gate7 lifecycle, attribution,
> regression baselines, and release-state semantics.

## Why Another Agent Project?

Every agent framework solves the same problem: *how do I build and orchestrate
an agent?* LangChain, CrewAI, AutoGen — they all answer *"how do I make the
agent do X."*

agent-prod answers a different question: *"how do I know this agent run is
safe to ship to production?"*

This is not an eval problem. Eval frameworks score one dimension (answer
correctness) in a vacuum. Production safety is multi-dimensional: the tool
call that reads `/etc/shadow` is unsafe even if the final answer is correct.
The budget blowout that costs $200 in one session is unsafe even if every
answer is high quality.

agent-prod models production risk as a **pipeline of gates** — sequential,
independent checks, each with its own rollback, where the **first failure**
rejects the run. This is the fundamental architecture decision.

## Architecture

```
                         QualityGateEngine
                         ─────────────────
                              │
    ┌─────────────────────────┼────────────────────────────┐
    │                         │                            │
    ▼                         ▼                            ▼
 ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐
 │Gate0  │  │Gate1  │  │Gate2  │  │Gate3  │  │Gate4  │  │Gate5  │  │Gate6  │  │Gate7  │
 │Perm.  │→│Budget │→│Trace  │→│Regr.  │→│Gray   │→│Audit  │→│Answer │→│Exec.  │
 │       │  │       │  │       │  │       │  │       │  │       │  │Quality│  │Cons.  │
 ├───────┤  ├───────┤  ├───────┤  ├───────┤  ├───────┤  ├───────┤  ├───────┤  ├───────┤
 │ACL    │  │Token  │  │DAG    │  │Baseline│  │Stage  │  │Policy │  │Check- │  │Plan   │
 │check  │  │budget │  │verify │  │compare│  │1→100% │  │rules  │  │list   │  │align  │
 └───────┘  └───────┘  └───────┘  └───────┘  └───────┘  └───────┘  └───────┘  └───────┘
      │          │          │          │          │          │          │          │
      └──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
                                      │
                                      ▼
                              ┌──────────────┐
                              │ Improvement  │  ← persistent state machine
                              │ Status:      │
                              │ production   │
                              │ rejected     │
                              │ rolled_back  │
                              └──────────────┘
```

### The Improvement State Machine

Every run submitted to agent-prod is an `Improvement` — a Pydantic model that
moves through states:

```
CANDIDATE ──▶ GATE1_PASSED ──▶ ... ──▶ PRODUCTION
                                        │
                                     REJECTED ──▶ ROLLED_BACK
```

The engine runs gates in order. Each gate can:

- **Pass** → advance the state machine to the next state
- **Fail** → rollback (undo side effects), set state to REJECTED, dispatch alert
- **Timeout** → treated as fail (configurable threshold)

This is not a scoring system. There is no "aggregate score." A run that fails
Gate0 never reaches Gate1.

### Design Rationale: Sequential Fail-Fast

Why not run all gates in parallel and aggregate?

1. **Cost.** Gate1 checks token budgets — if the run already spent $200, why
   bother checking answer quality?
2. **Causality.** Gate5 (audit) checks that Gate1–Gate4 all passed. Parallel
   gates can't express this dependency.
3. **Rollback.** Each gate can roll back partial side effects. If Gate4 (gray
   release) pushed traffic to a canary and Gate5 (audit) fails, Gate4 rolls
   back the canary. Sequential execution makes rollback deterministic.
4. **Feedback.** The caller gets exactly one failure reason — `"Gate3: latency
   degraded 40% from baseline"` — not a laundry list of every imperfection.

## GatePlugin ABC — The Interface Standard

The core design decision: the engine interacts with gates *exclusively through
an abstract base class*. It never imports concrete gate classes.

```python
class GatePlugin(ABC):
    name: GateName                  # Unique identifier
    rollback_level: RollbackLevel   # Severity of rollback needed

    @abstractmethod
    def verify(self, improvement: Improvement) -> GateResult:
        """Run this gate's checks. Must be stateless."""
        ...

    @abstractmethod
    def rollback(self, improvement: Improvement) -> None:
        """Undo side effects from this gate's verify()."""
        ...

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict, name: GateName) -> GatePlugin:
        """Factory: create a gate from YAML/JSON config."""
        ...
```

### Why This Matters

**Replaceability.** Any gate can be swapped without touching the engine.
Gate1's budget algorithm is an implementation detail — the engine only knows
`verify(improvement) → GateResult`.

**Third-party gates.** A team can write `CompanyPolicyGate(GatePlugin)` in
their own repo, register it, and `from_yaml()` picks it up. No fork, no
monkey-patch.

**Testability.** Every gate is tested in isolation by creating an Improvement
and calling `verify()`. No engine needed.

**The plugin boundary also defines the project's contribution surface:**
to add a new quality dimension, you implement 3 methods. That's it.

### Current Gate Implementations

| Gate | Class | Logic |
|---|---|---|
| Gate0 | `Gate0Permission` | Tool ACL + YAML risk classification + argument inspection |
| Gate1 | `Gate1Execution` | Token/time budget per agent type + circuit breaker |
| Gate2 | `Gate2TraceIntegrity` | DAG validation: every tool_call must map to an llm_call |
| Gate3 | `Gate3Regression` | Numeric comparison vs. evolving baseline + DeepDiff |
| Gate4 | `Gate4GrayRelease` | Progressive rollout: 1% → 10% → 50% → 100% |
| Gate5 | `Gate5ReleaseAudit` | Policy-as-code: 6 rules, enforce/observe modes |
| Gate6 | `Gate6AnswerQuality` | Checklist (12 binary checks), LLM-as-judge, exact-match, semantic |
| Gate7 | `Gate7ExecutionConsistency` | Plan-to-output alignment |

## Closed-Loop Topology vs. Static Eval

```
Static eval:                           Agent-prod closed loop:

input ──▶ agent ──▶ output             input ──▶ agent ──▶ output
           │                                            │
           ▼                                            ▼
       evaluator ──▶ score                     gate pipeline ──▶ status
                                                     │
                                           ┌─────────┴─────────┐
                                           │                   │
                                      production            rejected
                                                              │
                                                              ▼
                                                     rollback + feedback
```

### Static eval (conventional)

- Single pass: submit output, get a score
- No state, no rollback, no audit trail
- Answers "how good is this output?"
- Used for: offline benchmark, model comparison

### Closed-loop gates (agent-prod)

- Multi-pass: each gate can reject before the next runs
- Full state machine: CANDIDATE → PRODUCTION / REJECTED / ROLLED_BACK
- Answers "is this run safe for production?"
- Used for: pre-release gating, canary analysis, regression detection

### When You Need Both

You need eval **and** gates. Eval tells you "model B scores 0.05 higher than
model A." Gates tell you "model B called an undeclared tool — block." They
are complementary, not competitors.

## Configuration as Code

agent-prod reads a single `config.yaml` that defines all gate parameters:

```yaml
gates:
  gate0:
    per_agent:
      my-agent:
        mode: observe        # observe | enforce — start safe
  gate1:
    budgets:
      default:
        token_budget: 10000
        time_budget_ms: 600000
  gate3:
    regress_pct: 0.95        # 95% of baseline → still OK
    perf_degradation_limit: 0.05
    auto_evolve_baseline: true  # production runs become new baselines
  gate5:
    mode: observe            # skip human approval in dev
  gate6:
    evaluator: checklist
    pass_threshold: 0.58     # 7/12 checklist items
    per_agent:
      claude-code:
        pass_threshold: 0.67 # 8/12 for high-quality agents
```

The engine loads this via `from_yaml()` and configures every gate from it.
Gate-specific configs (`Gate1Config`, `Gate3Config`, `Gate5Config`, etc.) are
dataclasses with `from_yaml()` factory methods.

## Persistence and Repository

Every Improvement is persisted. Three backends:

| Backend | When to use |
|---|---|
| `MemoryRepository` | Testing, demo |
| `FileRepository` | Single-node, <10K records |
| `PostgresRepository` | Production, multi-instance |

The `ImprovementRepository` abstract interface ensures storage is swappable.
The engine calls `repository.save(improvement)` after each gate and on final
status — crashes never lose progress.

## Telemetry and Observability

- **Structured logging** via `structlog` (falls back to stdlib logging)
- **Prometheus metrics** (optional): per-gate latency histograms, rejection
  counters, circuit-breaker state
- **OpenTelemetry** (optional): trace propagation across gates
- **Alerts**: webhook, Slack, Discord, Telegram dispatchers — configurable
  per-gate failure

## Dogfood: We Ship Through Our Own Gates

All 217 Hermes traces in the [dogfood report](DOGFOOD_REPORT.md) ran through
the full pipeline. The report documents:

- 70% pass rate in observe mode
- 31% false positive rate (Gate3 baseline compatibility — since fixed)
- 0% missed detections on 20-trace spot check
- Per-gate latency breakdown (Gate2 is the bottleneck at ~5s)

Every PR that modifies the gate logic triggers the full CI suite (194 tests).
The project's own CI badge reflects its own quality gates.

## Development

```bash
git clone https://github.com/fangzheng698-lang/agent-prod.git
cd agent-prod
pip install -e ".[dev]"
python -m pytest tests/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow.

## Related

- [GatePlugin ABC source](/src/agent_prod/gates/interface.py) — the 30-line
  interface that defines how every gate works
- [Dogfood report](DOGFOOD_REPORT.md) — we ate our own dog food
- [Comparison analysis](COMPARISON_ANALYSIS.md) — how agent-prod relates to
  other agent ecosystem projects
- [MCP integration](docs/MCP_INTEGRATION.md) — using agent-prod from Claude
  Desktop, Cursor, Cline
- [Calibration guide](docs/CALIBRATION.md) — tuning Gate5/Gate6 thresholds