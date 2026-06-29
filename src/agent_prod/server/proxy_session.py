"""Multi-turn proxy session accumulation.

Each connected agent window gets a ProxySession that:
  - Accumulates decisions/tool_calls per LLM turn
  - Maintains heartbeat for crash detection
  - Tracks agent metadata (type, version, declared_tools)
  - Supports session finalization on explicit end or timeout
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CRASHED = "crashed"
    TIMEOUT = "timeout"
    EVALUATING = "evaluating"


@dataclass
class ProxySession:
    """In-memory representation of a proxy agent session."""

    session_id: str
    agent_type: str
    version: str = ""
    model: str = ""
    status: SessionStatus = SessionStatus.ACTIVE
    declared_tools: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    tool_defs: list[dict] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_duration_ms: float = 0.0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    final_output: str = ""
    gate_result: dict | None = None
    error: str = ""

    @property
    def accumulated_decisions(self) -> int:
        return len(self.decisions)

    def record_turn(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        tool_calls: list[dict],
        latency_ms: float,
    ) -> None:
        """Accumulate one turn/decision into this session."""
        decision = {
            "decision_id": f"{self.session_id}-turn-{len(self.decisions) + 1}",
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tool_calls": tool_calls,
        }
        self.decisions.append(decision)
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_duration_ms += latency_ms
        self.last_seen = time.time()

    def set_declared_tools(self, tools: list[str], tool_defs: list[dict]) -> None:
        self.declared_tools = tools
        self.tool_defs = tool_defs

    def is_stale(self, timeout_seconds: float = 120.0) -> bool:
        return (time.time() - self.last_seen) > timeout_seconds

    def build_final_trace(self) -> dict:
        """Build an AgentTrace-compatible dict for the gate pipeline."""
        return {
            "agent": self.agent_type,
            "version": self.version,
            "session_id": self.session_id,
            "output": {"final_response": self.final_output},
            "decisions": self.decisions,
            "declared_tools": self.declared_tools,
            "current_metrics": {
                "latency_p95_ms": self.total_duration_ms,
                "success_rate": 1.0,
                "error_rate": 0.0,
                "tokens_total": self.total_prompt_tokens + self.total_completion_tokens,
                "custom": {
                    "agent_version": self.version,
                    "total_turns": len(self.decisions),
                    "model": self.model,
                },
            },
            "metadata": {
                "source": "proxy_monitor",
                "total_duration_ms": self.total_duration_ms,
            },
        }

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_type": self.agent_type,
            "version": self.version,
            "model": self.model,
            "status": self.status.value,
            "declared_tools": self.declared_tools,
            "decisions_count": len(self.decisions),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_duration_ms": self.total_duration_ms,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "final_output": self.final_output[:200] if self.final_output else "",
            "gate_result": self.gate_result,
            "error": self.error,
        }


class ProxySessionManager:
    """Manages all active proxy sessions (one per connected agent window).

    Thread-safe via a lock. Sessions are tracked in-memory for fast access,
    and periodically persisted to SQLite (StateStore) for durability.
    """

    def __init__(self, state_store=None):
        self._sessions: dict[str, ProxySession] = {}
        self._lock = threading.Lock()
        self._store = state_store

    def get_or_create(
        self,
        session_id: str,
        agent_type: str,
        version: str = "",
        model: str = "",
    ) -> ProxySession:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing:
                # Re-activate crashed sessions that came back to life
                if existing.status in (SessionStatus.CRASHED, SessionStatus.TIMEOUT):
                    existing.status = SessionStatus.ACTIVE
                existing.last_seen = time.time()
                return existing

            session = ProxySession(
                session_id=session_id,
                agent_type=agent_type,
                version=version,
                model=model,
            )
            self._sessions[session_id] = session
            self._persist(session)
            logger.info(
                "Proxy session created: %s (agent=%s, model=%s)",
                session_id, agent_type, model,
            )
            return session

    def get(self, session_id: str) -> ProxySession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def finalize(
        self,
        session_id: str,
        status: SessionStatus,
        output: str = "",
        error: str = "",
    ) -> ProxySession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            session.status = status
            session.last_seen = time.time()
            if output:
                session.final_output = output
            if error:
                session.error = error
            self._persist(session)
            return session

    def list_active(self) -> list[ProxySession]:
        with self._lock:
            return [s for s in self._sessions.values() if s.status == SessionStatus.ACTIVE]

    def list_all(self) -> list[dict]:
        with self._lock:
            return [s.to_dict() for s in self._sessions.values()]

    def detect_stale(self, timeout_seconds: float = 120.0) -> list[ProxySession]:
        """Find sessions that haven't had activity in ``timeout_seconds``."""
        stale: list[ProxySession] = []
        with self._lock:
            now = time.time()
            for s in list(self._sessions.values()):
                if s.status == SessionStatus.ACTIVE and (now - s.last_seen) > timeout_seconds:
                    s.status = SessionStatus.CRASHED
                    s.error = f"crash detected: no activity for {now - s.last_seen:.0f}s"
                    self._persist(s)
                    stale.append(s)
        return stale

    def pop_for_evaluation(self) -> list[ProxySession]:
        """Remove and return sessions ready for evaluation (completed or crashed)."""
        ready: list[ProxySession] = []
        with self._lock:
            final_stati = {
                SessionStatus.COMPLETED,
                SessionStatus.CRASHED,
                SessionStatus.TIMEOUT,
            }
            to_remove = [
                sid
                for sid, s in self._sessions.items()
                if s.status in final_stati and s.gate_result is None
            ]
            for sid in to_remove:
                s = self._sessions.pop(sid)
                ready.append(s)
        return ready

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def set_gate_result(self, session_id: str, gate_result: dict) -> None:
        """Store gate evaluation result and persist it.

        Called by the eval worker after a session has been evaluated.
        The session may have been popped from memory already, so this
        persists directly to SQLite.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.gate_result = gate_result
                self._persist(session)
                return
        # Session already popped — persist directly via store
        if self._store:
            try:
                meta = {
                    "gate_result": gate_result,
                    "status": "evaluated",
                    "proxy_session": True,
                }
                self._store._upsert_proxy_session(session_id, meta)
            except Exception as e:
                logger.warning("Failed to persist gate result for %s: %s", session_id, e)

    def _persist(self, session: ProxySession) -> None:
        """Write session snapshot to StateStore (best-effort)."""
        if not self._store:
            return
        try:
            meta = {
                "agent_type": session.agent_type,
                "version": session.version,
                "model": session.model,
                "status": session.status.value,
                "declared_tools": session.declared_tools,
                "decision_count": len(session.decisions),
                "total_prompt_tokens": session.total_prompt_tokens,
                "total_completion_tokens": session.total_completion_tokens,
                "total_duration_ms": session.total_duration_ms,
                "first_seen": session.first_seen,
                "last_seen": session.last_seen,
                "final_output": session.final_output[:5000] if session.final_output else "",
                "error": session.error,
                "gate_result": session.gate_result,
                "proxy_session": True,
            }
            self._store._upsert_proxy_session(session.session_id, meta)
        except Exception as e:
            logger.warning("Failed to persist proxy session %s: %s", session.session_id, e)