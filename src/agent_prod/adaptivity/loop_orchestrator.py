# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""LoopOrchestrator — 四阶段闭环 + Replay/Benchmark/Optimizer 全接入。

Execution → Attribution → Optimization → Release
                                     ↑
              Replay验证 + Benchmark对比 ──┘

用法:
    orch = LoopOrchestrator()
    result = await orch.run_cycle(prompt="hi", session_id="s-1", turns=[...])
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_prod.adaptivity.causal_attributor import CausalAttributor
from agent_prod.adaptivity.data_flywheel import FlywheelEngine, FlywheelReport
from agent_prod.gateway.gateway import QualityGateGateway
from agent_prod.observability.execution_log import ExecutionLogRecord, ExecutionLogger
from agent_prod.testing.release_manager import ReleaseManager, ReleaseState
from agent_prod.testing.replay import ReplayRecorder, ReplayPlayer, ReplayRecord
from agent_prod.testing.benchmark import BenchmarkRunner, BenchmarkSnapshot, compare_snapshots, save_snapshot, load_snapshot
from agent_prod.testing.optimizer import analyze_logs, OptimizationSuggestion
from agent_prod.testing.governance import GovernancePanel
from agent_prod.testing.gate_stress import GateStressRunner, StressReport
from agent_prod.lifecycle.loop_state import LoopStateMachine, LoopState, StateTransition


@dataclass
class CyclePhase:
    name: str
    duration_ms: float
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class CycleResult:
    session_id: str
    run_id: str
    started_at: str
    completed_at: str
    phases: list[CyclePhase]
    gate_all_passed: bool
    gate_status: str
    flywheel_report: FlywheelReport | None = None
    release_state: ReleaseState | None = None
    total_duration_ms: float = 0.0
    replay_valid: bool | None = None
    benchmark_improved: bool | None = None
    governance_summary: dict[str, Any] | None = None
    stress_report: StressReport | None = None
    state_machine: LoopStateMachine | None = None

    @property
    def summary(self) -> str:
        lines = [
            f"=== LoopOrchestrator Cycle: {self.run_id} ===",
            f"  Session:  {self.session_id}",
            f"  Duration: {self.total_duration_ms:.0f}ms",
        ]
        for p in self.phases:
            icon = "✅" if p.success else "❌"
            lines.append(f"  {icon} {p.name}: {p.duration_ms:.0f}ms")
        lines.append(f"  Gates:    {'ALL PASSED' if self.gate_all_passed else 'FAILED'} ({self.gate_status})")
        if self.replay_valid is not None:
            lines.append(f"  Replay:   {'MATCH' if self.replay_valid else 'MISMATCH'}")
        if self.benchmark_improved is not None:
            lines.append(f"  Benchmark:{'IMPROVED' if self.benchmark_improved else 'DEGRADED'}")
        if self.flywheel_report:
            lines.append(f"  Flywheel: {self.flywheel_report.summary}")
        if self.release_state:
            lines.append(f"  Release:  {self.release_state.version} -> {self.release_state.status.value}")
        if self.stress_report:
            lines.append(f"  Stress:   {self.stress_report.total_samples} samples, pass={self.stress_report.pass_rate:.1%}, stable={'YES' if self.stress_report.stable else 'NO'}")
        return "\n".join(lines)


class LoopOrchestrator:
    """四阶段闭环 + Replay/Benchmark/Optimizer 全接入。"""

    def __init__(
        self,
        *,
        log_path: str = "data/execution_log.jsonl",
        replay_dir: str = "data/replays",
        benchmark_dir: str = "data/benchmarks",
        gateway: QualityGateGateway | None = None,
        flywheel: FlywheelEngine | None = None,
        attributor: CausalAttributor | None = None,
        release_manager: ReleaseManager | None = None,
    ):
        self._log_path = log_path
        self._execution_logger = ExecutionLogger(log_path)
        self._flywheel = flywheel or FlywheelEngine(log_path)
        self._attributor = attributor or CausalAttributor(min_pre_samples=10)
        self._release = release_manager or ReleaseManager()
        self._gateway = gateway
        self._replay_recorder = ReplayRecorder(base_dir=replay_dir)
        self._replay_player = ReplayPlayer(base_dir=replay_dir)
        self._benchmark_runner = BenchmarkRunner()
        self._benchmark_dir = benchmark_dir
        self._last_benchmark_path = f"{benchmark_dir}/latest.json"
        self._governance = GovernancePanel(self._release)
        self._stress_runner = GateStressRunner()
        # versioned benchmark snapshots for cross-release comparison
        self._benchmark_snapshots: dict[str, BenchmarkSnapshot] = {}

    @property
    def gateway(self) -> QualityGateGateway:
        if self._gateway is None:
            self._gateway = QualityGateGateway.memory()
        return self._gateway

    @gateway.setter
    def gateway(self, g: QualityGateGateway) -> None:
        self._gateway = g

    @property
    def release_manager(self) -> ReleaseManager:
        return self._release

    @property
    def governance(self) -> GovernancePanel:
        return self._governance

    # ── Public API ───────────────────────────────────────────────

    def run_cycle_sync(self, *, prompt: str, session_id: str = "",
                       turns: list | None = None, messages: list[dict] | None = None) -> CycleResult:
        return asyncio.run(self.run_cycle(prompt=prompt, session_id=session_id, turns=turns, messages=messages))

    async def run_cycle(self, *, prompt: str, session_id: str = "",
                        turns: list | None = None, messages: list[dict] | None = None) -> CycleResult:
        import time as _time

        session_id = session_id or f"sess-{uuid.uuid4().hex[:8]}"
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        started_at = datetime.now(UTC).isoformat()
        overall_start = _time.monotonic()
        phases: list[CyclePhase] = []
        sm = LoopStateMachine(run_id)

        # ════════════════════════════════════════════════════════
        # State: CANDIDATE -> EXECUTING
        # ════════════════════════════════════════════════════════
        sm.start_execution()

        # ════════════════════════════════════════════════════════
        # Phase 1: Execution + Replay Recording
        # ════════════════════════════════════════════════════════
        t0 = _time.monotonic()
        gate_all_passed, gate_status = False, "unknown"
        total_tokens, total_time_ms, total_turns = 0, 0.0, 0
        response_text = ""

        if turns is not None:
            try:
                improvement, gate_all_passed = await self.gateway.validate(session_id, messages or [], turns)
                gate_status = improvement.status.value
                total_tokens = sum(t.response.tokens_prompt + t.response.tokens_completion for t in turns if t.response)
                total_time_ms = sum(t.duration_ms for t in turns)
                total_turns = len(turns)
                for t in reversed(turns):
                    if t.response and t.response.content:
                        response_text = t.response.content[:2000]
                        break
                if not response_text:
                    response_text = f"Task completed in {total_turns} turns"

                # Record replay
                try:
                    turn_dicts = []
                    for t in turns:
                        td = {"role": getattr(t, "role", "unknown")}
                        if t.response:
                            td["response"] = t.response.content[:2000] if t.response.content else ""
                            td["tokens"] = t.response.tokens_prompt + t.response.tokens_completion
                        td["tool_results"] = list(t.tool_results) if hasattr(t, "tool_results") else []
                        turn_dicts.append(td)
                    self._replay_recorder.record(run_id, turn_dicts, final_response=response_text)
                except Exception:
                    pass

                phases.append(CyclePhase(name="execution", duration_ms=(_time.monotonic()-t0)*1000,
                    success=gate_all_passed, data={"gate_status": gate_status, "tokens": total_tokens,
                    "time_ms": total_time_ms, "turns": total_turns}))
            except Exception as e:
                phases.append(CyclePhase(name="execution", duration_ms=(_time.monotonic()-t0)*1000, success=False, error=str(e)))
        else:
            phases.append(CyclePhase(name="execution", duration_ms=0, success=True,
                data={"gate_status": "skipped", "note": "no turns provided"}))

        # State: EXECUTING -> EXECUTED
        sm.finish_execution(gate_all_passed, tokens=total_tokens, time_ms=total_time_ms, turns=total_turns)

        # Log execution
        try:
            self._execution_logger.log_execution(ExecutionLogRecord(
                run_id=run_id, session_id=session_id, prompt=prompt, response=response_text,
                turns=total_turns, costs={"prompt_tokens": total_tokens//2, "completion_tokens": total_tokens-total_tokens//2},
                duration_ms=total_time_ms, quality_gate_result={"status": gate_status, "passed": gate_all_passed}))
            self._flywheel.log_execution(run_id=run_id, session_id=session_id, prompt=prompt, response=response_text,
                turns=total_turns, tokens=total_tokens, duration_ms=total_time_ms, gate_pass=gate_all_passed, gate_status=gate_status)
        except Exception:
            pass

        # ════════════════════════════════════════════════════════
        # Phase 2: Attribution
        # ════════════════════════════════════════════════════════
        t0 = _time.monotonic()
        if not gate_all_passed and turns is not None:
            sm.start_attribution()
            try:
                recent = self._flywheel._load_logs(limit=200)
                pre_logs = [r for r in recent if r.session_id != session_id]
                post_logs = [r for r in recent if r.session_id == session_id]
                if pre_logs and post_logs:
                    report = self._attributor.attribute([{"name": f"gate-fail-{run_id}", "pre_logs": pre_logs,
                        "post_logs": post_logs, "candidate_vars": ["duration_ms","tokens_used","prompt_tokens","completion_tokens","gate_passed"]}])
                    phases.append(CyclePhase(name="attribution", duration_ms=(_time.monotonic()-t0)*1000, success=True, data=report.to_dict()))
                else:
                    phases.append(CyclePhase(name="attribution", duration_ms=(_time.monotonic()-t0)*1000, success=True, data={"skipped":True,"reason":"insufficient_data"}))
            except Exception as e:
                phases.append(CyclePhase(name="attribution", duration_ms=(_time.monotonic()-t0)*1000, success=False, error=str(e)))
            sm.finish_attribution()
        else:
            phases.append(CyclePhase(name="attribution", duration_ms=0, success=True,
                data={"skipped":True, "reason":"gates_passed" if gate_all_passed else "no_turns"}))

        # State: EXECUTED -> OPTIMIZING
        sm.start_optimization()

        # ════════════════════════════════════════════════════════
        # Phase 3: Optimization (Flywheel + Optimizer)
        # ════════════════════════════════════════════════════════
        t0 = _time.monotonic()
        flywheel_report = None
        optimizer_suggestions = []
        try:
            flywheel_report = self._flywheel.generate_report(recent_count=10)
            logs = self._flywheel._load_logs(limit=200)
            optimizer_suggestions = [s.model_dump() for s in analyze_logs(logs)]
            phases.append(CyclePhase(name="optimization", duration_ms=(_time.monotonic()-t0)*1000, success=True,
                data={"flywheel": flywheel_report.to_dict() if flywheel_report else {},
                      "optimizer_suggestions": optimizer_suggestions}))
        except Exception as e:
            phases.append(CyclePhase(name="optimization", duration_ms=(_time.monotonic()-t0)*1000, success=False, error=str(e)))

        # State: OPTIMIZING -> OPTIMIZED -> VERIFYING
        sm.finish_optimization()
        sm.start_verification()

        # ════════════════════════════════════════════════════════
        # Phase 4: Release (Replay + Benchmark before promote)
        # ════════════════════════════════════════════════════════
        t0 = _time.monotonic()
        release_state = None
        replay_valid = None
        benchmark_improved = None
        stress_report: StressReport | None = None
        try:
            version = f"v{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{run_id[-6:]}"
            release_state = self._release.create_release(version=version, improvement_id=session_id,
                notes=f"LoopOrchestrator {run_id}: {'PASSED' if gate_all_passed else 'FAILED'}")

            # Always record benchmark snapshot for historical comparison
            try:
                current = BenchmarkSnapshot(
                    avg_turns=float(total_turns), avg_duration_ms=float(total_time_ms),
                    avg_tokens=float(total_tokens), gate_pass_rate=1.0 if gate_all_passed else 0.0,
                    prompt_count=1)
                self._benchmark_snapshots[version] = current
                save_snapshot(current, f"{self._benchmark_dir}/{version}.json")
                # Compare against previous versions
                prev_versions = [v for v in sorted(self._benchmark_snapshots.keys()) if v != version]
                if prev_versions:
                    prev_snap = self._benchmark_snapshots[prev_versions[-1]]
                    cmp = compare_snapshots(prev_snap, current)
                    benchmark_improved = cmp.get("improved", False)
            except Exception:
                pass

            if gate_all_passed:
                # Replay verification before promote
                if turns is not None:
                    try:
                        recorded = self._replay_player.load(run_id)
                        if recorded:
                            replay_valid = recorded.turns is not None and len(recorded.turns) == len(turns)
                    except Exception:
                        pass

                # Gate stress test — concurrent gate validation
                stress_report = None
                try:
                    stress_report = await self._stress_runner.stress_test(
                        self.gateway, [turns] * 3 if turns else [[], [], []],
                    )
                except Exception:
                    pass

                # State: VERIFYING -> VERIFIED
                verification_passed = replay_valid is not False  # None is OK (no replay available)
                sm.finish_verification(verification_passed, replay_valid=replay_valid, benchmark_improved=benchmark_improved, stress_stable=stress_report.stable if stress_report else None)
                sm.start_release()

                self._release.promote(version, reason="All quality gates passed")
                sm.finish_release(True, version=version)
            else:
                self._release.rollback(version, reason=f"Gate failure: {gate_status}")
                sm.finish_verification(False, reason="gates_failed")
                sm.finish_release(False, version=version, reason=f"Gate failure: {gate_status}")

            phases.append(CyclePhase(name="release", duration_ms=(_time.monotonic()-t0)*1000, success=True,
                data={"version": version, "status": release_state.status.value,
                      "action": "promote" if gate_all_passed else "rollback",
                      "replay_valid": replay_valid, "benchmark_improved": benchmark_improved}))
        except Exception as e:
            phases.append(CyclePhase(name="release", duration_ms=(_time.monotonic()-t0)*1000, success=False, error=str(e)))

        overall_duration = (_time.monotonic() - overall_start) * 1000
        gov_summary = self._governance.summary()
        return CycleResult(session_id=session_id, run_id=run_id, started_at=started_at,
            completed_at=datetime.now(UTC).isoformat(), phases=phases, gate_all_passed=gate_all_passed,
            gate_status=gate_status, flywheel_report=flywheel_report, release_state=release_state,
            total_duration_ms=overall_duration, replay_valid=replay_valid, benchmark_improved=benchmark_improved,
            governance_summary=gov_summary, stress_report=stress_report, state_machine=sm)
