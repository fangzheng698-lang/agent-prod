"""
Hermes Plugin: agent-prod

Real-time quality gates + data flywheel intercepting Hermes execution.

Hooks:
  pre_tool_call        → safety gate: block dangerous tool calls
  post_tool_call       → data flywheel: log every tool execution
  transform_llm_output → quality gate: score/gate the final response
  on_session_start     → init per-turn accumulator
  on_session_end       → flush turn summary + persist to postgres
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("plugins.agent-prod")

# ── Execution log path (data flywheel) ────────────────────────────
DATA_DIR = Path("/root/experiment/agent-prod/data")
EXECUTION_LOG = DATA_DIR / "execution_log.jsonl"
EXECUTION_LOG.parent.mkdir(parents=True, exist_ok=True)

# ── Per-turn accumulator (module-level, single-agent process) ─────
_turn: Dict[str, Any] = {"tool_calls": [], "blocked_calls": [], "gate_results": []}
_session_start_ts: float = 0.0
_gates_available: Optional[bool] = None
_engine_ref: Any = None  # cached engine with PostgresRepository for persistence


def _ensure_gates_loaded() -> bool:
    """Lazy-import agent-prod gates. Result cached for process lifetime."""
    global _gates_available
    if _gates_available is not None:
        return _gates_available
    try:
        # structlog causes weakref errors in threaded hook callbacks.
        # Inject a structlog-compatible shim that delegates to stdlib logging.
        import sys

        class _FakeStructlog:
            @staticmethod
            def get_logger(name):
                stdlib_logger = logging.getLogger(name)

                class _Shim:
                    """structlog-compatible logger that delegates to stdlib."""
                    @staticmethod
                    def info(**event):
                        msg = event.pop("event", "")
                        extra = " ".join(f"{k}={v}" for k, v in event.items())
                        stdlib_logger.info("%s | %s" if extra else "%s", msg, extra)

                    @staticmethod
                    def warning(**event):
                        msg = event.pop("event", "")
                        extra = " ".join(f"{k}={v}" for k, v in event.items())
                        stdlib_logger.warning("%s | %s" if extra else "%s", msg, extra)

                    @staticmethod
                    def error(**event):
                        msg = event.pop("event", "")
                        extra = " ".join(f"{k}={v}" for k, v in event.items())
                        stdlib_logger.error("%s | %s" if extra else "%s", msg, extra)

                    @staticmethod
                    def debug(**event):
                        msg = event.pop("event", "")
                        extra = " ".join(f"{k}={v}" for k, v in event.items())
                        stdlib_logger.debug("%s | %s" if extra else "%s", msg, extra)

                return _Shim()

        sys.modules["structlog"] = _FakeStructlog

        from agent_prod.gates.engine import QualityGateEngine  # noqa: F401
        from agent_prod.gates.models import Improvement, GateResult, GateName  # noqa: F401
        _gates_available = True
        logger.info("[agent-prod] agent-prod gates imported successfully")
    except ImportError as e:
        logger.warning("[agent-prod] gates not available: %s", e)
        _gates_available = False
    return _gates_available


# ── Safety patterns (regex-based, avoids false positives on paths) ─
import re

DESTRUCTIVE_COMMANDS = [
    # Only match "rm -rf /" when followed by space, newline, semicolon, pipe, or end
    (re.compile(r"rm\s+-rf\s+/(?:\s|;|\||&|$)"), "rm -rf /"),
    (re.compile(r"rm\s+-rf\s+~(?:\s|;|\||&|$)"), "rm -rf ~"),
    (re.compile(r"rm\s+-rf\s+\$HOME(?:\s|;|\||&|$)"), "rm -rf $HOME"),
    (re.compile(r"rm\s+-rf\s+/\*"), "rm -rf /*"),
    (re.compile(r"\bmkfs\.\w+"), "mkfs"),
    (re.compile(r"dd\s+if="), "dd if="),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;:"), "fork bomb"),
    (re.compile(r">\s*/dev/sd[a-z]"), "> /dev/sdX"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "chmod -R 777 /"),
    (re.compile(r"chown\s+-R\s+\S+\s+/(?:etc|bin|lib|usr|var|sbin|boot)(?:\s|;|$)"), "chown -R on system dir"),
]

SENSITIVE_WRITE_PATHS = [
    "/etc/passwd",
    "/etc/shadow",
    "/etc/ssh",
    "/root/.ssh/id_rsa",
    "/root/.ssh/id_ed25519",
    "/root/.hermes/.env",
    "/root/.hermes/config.yaml",
]


def _check_safety(tool_name: str, args: dict) -> Optional[str]:
    """Check if a tool call should be blocked. Returns block reason or None."""
    if tool_name == "terminal":
        command = str(args.get("command", ""))
        for pattern, label in DESTRUCTIVE_COMMANDS:
            if pattern.search(command):
                return f"Blocked destructive pattern: {label}"

    if tool_name in ("write_file", "patch"):
        filepath = str(args.get("path", ""))
        for sp in SENSITIVE_WRITE_PATHS:
            if filepath == sp or filepath.startswith(sp):
                return f"Blocked write to sensitive path: {filepath}"

    return None


# ── Hook: on_session_start ─────────────────────────────────────────
def _on_session_start(**kwargs):
    global _turn, _session_start_ts
    _session_start_ts = time.time()
    _turn = {
        "session_id": kwargs.get("session_id", ""),
        "platform": kwargs.get("platform", "cli"),
        "tool_calls": [],
        "blocked_calls": [],
        "gate_results": [],
        "start_ts": datetime.now(timezone.utc).isoformat(),
    }


# ── Hook: pre_tool_call ────────────────────────────────────────────
def _pre_tool_call(tool_name: str, args: dict, task_id: str = "", **kwargs):
    """Safety gate: block dangerous tool calls before execution."""
    reason = _check_safety(tool_name, args)
    if reason is not None:
        logger.warning("[agent-prod] BLOCKED %s: %s", tool_name, reason)
        _turn["blocked_calls"].append({
            "tool": tool_name,
            "args": json.dumps(args, default=str)[:200],
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return {"action": "block", "message": f"[agent-prod safety gate] {reason}"}
    return None


# ── Hook: post_tool_call ───────────────────────────────────────────
def _post_tool_call(tool_name: str, args: dict, result: str,
                    task_id: str = "", duration_ms: int = 0, **kwargs):
    """Data flywheel: record every tool execution in real time."""
    _turn["tool_calls"].append({
        "tool": tool_name,
        "duration_ms": duration_ms,
        "result_len": len(result) if result else 0,
    })

    # Real-time append to execution log (non-blocking)
    try:
        record = {
            "session_id": task_id or _turn.get("session_id", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": "hermes",
            "turn_step": len(_turn["tool_calls"]),
            "tool": tool_name,
            "duration_ms": duration_ms,
            "result_len": len(result) if result else 0,
        }
        with open(EXECUTION_LOG, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


# ── Hook: transform_llm_output ─────────────────────────────────────
def _get_persist_engine():
    """Lazy-init a QualityGateEngine that uses PostgresRepository for persistence.

    Returns None if postgres is not configured or unavailable.
    """
    global _engine_ref, _gates_available
    if _engine_ref is not None:
        return _engine_ref

    if not _ensure_gates_loaded():
        _engine_ref = False
        return None

    try:
        from agent_prod.gates.engine import QualityGateEngine
        from agent_prod.gates.repository import PostgresRepository

        repo = PostgresRepository(
            dsn="postgresql://quality_gates:quality_gates@localhost:5432/quality_gates",
            pool_size=5,
        )
        _engine_ref = QualityGateEngine(repository=repo)
        logger.info("[agent-prod] Persist engine initialized (PostgresRepository)")
        return _engine_ref
    except Exception as e:
        logger.warning("[agent-prod] Persist engine init failed: %s", e)
        _engine_ref = False
        return None


def _transform_llm_output(response_text: str, session_id: str = "",
                          model: str = "", platform: str = "", **kwargs):
    """Quality gates: evaluate response quality before delivery.

    Hermes passes: response_text, session_id, model, platform.
    """
    if not _ensure_gates_loaded():
        return None  # pass through

    try:
        from agent_prod.gates.engine import QualityGateEngine
        from agent_prod.gates.models import Improvement

        # Defensive: ensure _turn has required keys
        tools = _turn.get("tool_calls", [])
        blocks = _turn.get("blocked_calls", [])

        tool_count = len(tools)
        blocked_count = len(blocks)
        total_ms = sum(c.get("duration_ms", 0) for c in tools)
        tool_names = list(set(c.get("tool", "?") for c in tools))

        # Build ExecutionOutput-compatible data for Gate1
        execution_output = {
            "final_response": response_text,
            "confidence": 0.9,
            "tools_used": tool_names,
            "token_count": len(response_text),
            "warnings": [
                f"blocked={blocked_count}" if blocked_count else "",
                f"duration_ms={total_ms}" if total_ms > 30000 else "",
            ],
        }
        execution_output["warnings"] = [w for w in execution_output["warnings"] if w]

        improvement = Improvement(
            name=f"hermes-turn-{session_id[-8:] if session_id else 'unknown'}",
            tool_calls=tools,
            baseline_output=execution_output,
            candidate_output=execution_output,  # same turn = no diff
            actual_time_ms=int(total_ms),
            actual_tokens=len(response_text),
            metadata={
                "session_id": session_id,
                "model": model,
                "platform": platform,
                "tool_count": tool_count,
                "blocked_count": blocked_count,
                "response_length": len(response_text),
            },
        )

        engine = QualityGateEngine()
        # Run gates 1-4 only (per-turn response quality).
        # Gate 5 (release audit with human approval) is for deployment decisions,
        # not individual turn interception.
        from agent_prod.gates.models import GateName as GN
        for gate_name in [GN.GATE1, GN.GATE2, GN.GATE3, GN.GATE4]:
            engine.run_gate(improvement, gate_name, persist=False)

        # Evaluate result
        failed = [r for r in improvement.gate_results if not r.passed]
        passed = len(failed) == 0
        gate_results = [
            {"gate": gr.gate_name.value, "passed": gr.passed, "reason": gr.reason}
            for gr in improvement.gate_results
        ]
        _turn["gate_results"] = gate_results

        # Save improvement reference for end-of-session persistence
        _turn["_improvement"] = improvement

        if not passed:
            fail_names = ", ".join(str(r.gate_name.value) for r in failed)
            fail_reasons = "; ".join(r.reason for r in failed)
            logger.warning("[agent-prod] GATE FAIL: %s — %s", fail_names, fail_reasons)
            warning = (
                f"\n⚠️  [agent-prod quality gate: {fail_names}] {fail_reasons}\n\n"
            )
            return warning + response_text

        logger.info("[agent-prod] Gates 1-4 passed: 4/4")

    except Exception as e:
        logger.warning("[agent-prod] gate evaluation error: %s", e)

    return None  # pass through unchanged


# ── Hook: on_session_end ───────────────────────────────────────────
def _on_session_end(**kwargs):
    """Flush turn summary to execution log + persist to postgres."""
    global _turn, _session_start_ts

    tools = _turn.get("tool_calls", [])
    blocks = _turn.get("blocked_calls", [])

    if not tools and not blocks:
        _turn = {"tool_calls": [], "blocked_calls": [], "gate_results": []}
        _session_start_ts = 0.0
        return

    duration_s = time.time() - _session_start_ts if _session_start_ts else 0
    total_ms = sum(c.get("duration_ms", 0) for c in tools)

    summary = {
        "session_id": _turn.get("session_id", "unknown"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "session_end",
        "tool_calls": len(tools),
        "blocked": len(blocks),
        "total_tool_duration_ms": int(total_ms),
        "total_session_duration_s": round(duration_s, 1),
        "gate_results": _turn.get("gate_results", []),
    }

    try:
        with open(EXECUTION_LOG, "a") as f:
            f.write(json.dumps(summary, default=str) + "\n")
        logger.info("[agent-prod] Turn flushed: %d tools, %.1fs, gates=%s",
                     len(tools), duration_s,
                     _turn.get("gate_results", []))
    except Exception as e:
        logger.warning("[agent-prod] flush error: %s", e)

    # Persist improvement to postgres
    improvement = _turn.get("_improvement")
    if improvement is not None:
        engine = _get_persist_engine()
        if engine is not False and engine is not None:
            try:
                # Update metadata with session-end info
                improvement.metadata["session_duration_s"] = round(duration_s, 1)
                improvement.metadata["total_tool_calls"] = len(tools)
                improvement.metadata["blocked_calls"] = len(blocks)
                improvement.metadata["event"] = "session_end"
                # Determine final status from gate results
                all_passed = all(
                    g.get("passed", False) for g in _turn.get("gate_results", [])
                    if isinstance(g, dict)
                )
                if all_passed and _turn.get("gate_results"):
                    from agent_prod.gates.models import ImprovementStatus
                    improvement.status = ImprovementStatus.PRODUCTION
                else:
                    from agent_prod.gates.models import ImprovementStatus
                    improvement.status = ImprovementStatus.REJECTED
                improvement.mark_updated()
                engine.repository.save(improvement)
                logger.info("[agent-prod] Persisted to postgres: %s -> %s",
                             improvement.id, improvement.status.value)
            except Exception as e:
                logger.warning("[agent-prod] postgres persist error: %s", e)

    _turn = {"tool_calls": [], "blocked_calls": [], "gate_results": []}
    _session_start_ts = 0.0


# ── Plugin entry point ─────────────────────────────────────────────
def register(ctx):
    """Register agent-prod hooks into the Hermes plugin system."""
    # Pre-import agent-prod gates in the main thread to avoid
    # structlog weakref issues in hook callback threads.
    _ensure_gates_loaded()

    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    logger.info("[agent-prod] Plugin registered: quality gates + data flywheel active")
