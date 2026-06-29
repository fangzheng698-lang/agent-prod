"""Phase 4.4: TaskRun 状态机 — PENDING→GATE_EVAL→APPROVED|REJECTED|ROLLED_BACK。

每次 Runtime 执行有清晰的状态流转，支撑后续飞轮闭环。

用法:
    from agent_prod.lifecycle.task_state import TaskState, TaskRun
    run = TaskRun(session_id="ses_001")
    run.transition(TaskState.RUNNING)
    ...
    run.transition(TaskState.APPROVED)
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

try:
    import structlog
    _logger = structlog.get_logger("task_state")
    _STRUCTLOG = True
except ImportError:
    _logger = logging.getLogger("task_state")
    _STRUCTLOG = False


class TaskState(str, Enum):
    """TaskRun 状态枚举。"""
    PENDING = "pending"           # 初始状态
    RUNNING = "running"           # Runtime 执行中
    GATE_EVAL = "gate_eval"       # 门禁评估中
    APPROVED = "approved"         # 全部通过
    REJECTED = "rejected"         # 门禁拒绝
    ROLLED_BACK = "rolled_back"   # 已回滚
    ERRORED = "error"             # 执行错误


# 合法状态转换
_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.RUNNING},
    TaskState.RUNNING: {TaskState.GATE_EVAL, TaskState.ERRORED},
    TaskState.GATE_EVAL: {TaskState.APPROVED, TaskState.REJECTED, TaskState.ERRORED},
    TaskState.APPROVED: set(),
    TaskState.REJECTED: {TaskState.ROLLED_BACK},
    TaskState.ROLLED_BACK: set(),
    TaskState.ERRORED: set(),
}


class InvalidTransition(Exception):
    """非法状态转换。"""

    def __init__(self, current: TaskState, target: TaskState):
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition from {current.value} to {target.value}")


class TaskRun:
    """一次 Runtime 执行的完整状态追踪。

    线程安全：单 session 使用，无需锁。
    """

    def __init__(
        self,
        session_id: str = "",
        run_id: str = "",
    ):
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.session_id = session_id
        self.state = TaskState.PENDING
        self.created_at = datetime.now(UTC)
        self.updated_at = self.created_at
        self.gate_status: str | None = None
        self.error: str | None = None
        self.metadata: dict[str, Any] = {}
        self._history: list[dict[str, Any]] = []

    def transition(self, target: TaskState, reason: str = "") -> None:
        """状态转换。非法转换抛出 InvalidTransition。"""
        if target not in _TRANSITIONS.get(self.state, set()):
            raise InvalidTransition(self.state, target)

        old_state = self.state
        self.state = target
        self.updated_at = datetime.now(UTC)

        entry = {
            "from": old_state.value,
            "to": target.value,
            "at": self.updated_at.isoformat(),
            "reason": reason,
        }
        self._history.append(entry)

        self._log_transition(old_state, target, reason)

    def mark_running(self) -> None:
        self.transition(TaskState.RUNNING, "Runtime execution started")

    def mark_gate_eval(self) -> None:
        self.transition(TaskState.GATE_EVAL, "Entering quality gate evaluation")

    def mark_approved(self, gate_status: str = "production") -> None:
        self.gate_status = gate_status
        self.transition(TaskState.APPROVED, f"Gates passed: {gate_status}")

    def mark_rejected(self, fail_gate: str = "", fail_reason: str = "") -> None:
        self.gate_status = f"rejected_at_{fail_gate}" if fail_gate else "rejected"
        self.error = fail_reason or "Gate rejected"
        self.transition(TaskState.REJECTED, f"Failed at {fail_gate}: {fail_reason}" if fail_gate else "Rejected")

    def mark_rolled_back(self, reason: str = "") -> None:
        self.transition(TaskState.ROLLED_BACK, reason or "Rolled back")

    def mark_error(self, error: str) -> None:
        self.error = error
        self.transition(TaskState.ERRORED, error)

    @property
    def is_terminal(self) -> bool:
        return self.state in (TaskState.APPROVED, TaskState.REJECTED,
                              TaskState.ROLLED_BACK, TaskState.ERRORED)

    @property
    def is_success(self) -> bool:
        return self.state == TaskState.APPROVED

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "gate_status": self.gate_status,
            "error": self.error,
            "history": self._history,
        }

    def _log_transition(self, old: TaskState, new: TaskState, reason: str) -> None:
        if _STRUCTLOG:
            _logger.info(
                event="task_state_transition",
                run_id=self.run_id,
                session_id=self.session_id,
                from_state=old.value,
                to_state=new.value,
                reason=reason,
            )
        else:
            _logger.info(
                "TaskRun %s: %s → %s (%s)", self.run_id, old.value, new.value, reason,
            )
