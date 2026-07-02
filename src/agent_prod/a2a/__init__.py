# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""A2A (Agent-to-Agent) — lightweight task delegation and result handoff.

Defines a minimal protocol for agent-to-agent communication:
  - Task delegation with capability negotiation
  - Result handoff with partial-success semantics
  - Error propagation with blame attribution
  - Adapters for LangChain / CrewAI / AutoGen

Not a standard — a lightweight adapter that works today.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Protocol types
# ═══════════════════════════════════════════════════════════════════


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"  # partial success — some subtasks succeeded


class ErrorCode(str, Enum):
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    INVALID_INPUT = "invalid_input"
    CAPABILITY_NOT_FOUND = "capability_not_found"
    INTERNAL_ERROR = "internal_error"
    DEPENDENCY_FAILED = "dependency_failed"


@dataclass
class A2AError:
    """Error with blame attribution — traceable to the failing agent."""

    code: ErrorCode = ErrorCode.UNKNOWN
    message: str = ""
    source_agent: str = ""       # which agent produced this error
    source_task: str = ""        # which task was running when error occurred
    cause: A2AError | None = None  # chain of underlying errors

    def to_dict(self) -> dict:
        d = {
            "code": self.code.value,
            "message": self.message[:200],
            "source_agent": self.source_agent,
            "source_task": self.source_task,
        }
        if self.cause:
            d["cause"] = self.cause.to_dict()
        return d


@dataclass
class A2ATask:
    """A task to be delegated to another agent."""

    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    required_capabilities: list[str] = field(default_factory=list)
    timeout_seconds: float = 120.0
    metadata: dict = field(default_factory=dict)

    # Output (set by executor)
    status: TaskStatus = TaskStatus.PENDING
    output: dict = field(default_factory=dict)
    error: A2AError | None = None
    completed_at: str = ""

    def to_dict(self) -> dict:
        d = {
            "id": self.id or f"task-{uuid.uuid4().hex[:8]}",
            "name": self.name,
            "input": self.input,
            "required_capabilities": self.required_capabilities,
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
            "status": self.status.value,
        }
        if self.output:
            d["output"] = self.output
        if self.error:
            d["error"] = self.error.to_dict()
        if self.completed_at:
            d["completed_at"] = self.completed_at
        return d


@dataclass
class A2AResult:
    """The result of executing one or more tasks."""

    task_id: str
    status: TaskStatus
    output: dict = field(default_factory=dict)
    error: A2AError | None = None
    child_results: list[A2AResult] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "output": self.output,
            "error": self.error.to_dict() if self.error else None,
            "child_results": [r.to_dict() for r in self.child_results],
            "duration_ms": round(self.duration_ms, 1),
        }


# ═══════════════════════════════════════════════════════════════════
# Agent interface
# ═══════════════════════════════════════════════════════════════════


class A2AAgent:
    """Interface for an agent that can receive delegated tasks.

    Implement this to make your agent compatible with A2A delegation.

    Usage:
        class MyAgent(A2AAgent):
            @property
            def capabilities(self):
                return ["web_search", "code_review"]

            def execute(self, task: A2ATask) -> A2AResult:
                # ... do the work ...
                return A2AResult(task_id=task.id, status=TaskStatus.SUCCEEDED, output={...})
    """

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @property
    def capabilities(self) -> list[str]:
        """What this agent can do. Used for capability negotiation."""
        return []

    def execute(self, task: A2ATask) -> A2AResult:
        """Execute a delegated task. Override this."""
        return A2AResult(
            task_id=task.id,
            status=TaskStatus.FAILED,
            error=A2AError(
                code=ErrorCode.CAPABILITY_NOT_FOUND,
                message=f"{self.name} does not implement execute()",
                source_agent=self.name,
                source_task=task.id,
            ),
        )


# ═══════════════════════════════════════════════════════════════════
# Delegator — routes tasks to capable agents
# ═══════════════════════════════════════════════════════════════════


class A2ADelegator:
    """Delegates tasks to registered agents based on capability matching.

    Usage:
        delegator = A2ADelegator()
        delegator.register(WebSearchAgent())
        delegator.register(CodeReviewAgent())

        task = A2ATask(name="search web", required_capabilities=["web_search"])
        result = delegator.delegate(task)
    """

    def __init__(self):
        self._agents: dict[str, A2AAgent] = {}

    def register(self, agent: A2AAgent) -> None:
        """Register an agent that can receive delegated tasks."""
        self._agents[agent.name] = agent
        logger.info("A2A: registered agent '%s' with capabilities: %s",
                     agent.name, agent.capabilities)

    def unregister(self, name: str) -> None:
        self._agents.pop(name, None)

    def find_agent(self, required_capabilities: list[str]) -> A2AAgent | None:
        """Find the best agent for a set of required capabilities."""
        if not required_capabilities:
            # No specific requirement — return first available
            for agent in self._agents.values():
                return agent
            return None

        # Score agents by capability overlap
        best_score = 0
        best_agent = None
        for agent in self._agents.values():
            score = sum(1 for cap in required_capabilities if cap in agent.capabilities)
            if score > best_score:
                best_score = score
                best_agent = agent

        if best_score == 0:
            logger.warning("A2A: no agent found for capabilities: %s", required_capabilities)
        return best_agent

    def delegate(self, task: A2ATask) -> A2AResult:
        """Delegate a task to the most capable agent.

        Returns A2AResult with partial-success semantics:
          - If the agent returns partial success, child_results contains
            the individual subtask results.
          - If the agent fails, error contains the chain of blame.
        """
        agent = self.find_agent(task.required_capabilities)
        if not agent:
            return A2AResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                error=A2AError(
                    code=ErrorCode.CAPABILITY_NOT_FOUND,
                    message=f"No agent found with capabilities: {task.required_capabilities}",
                    source_agent="delegator",
                    source_task=task.id,
                ),
            )

        logger.info("A2A: delegating task '%s' to agent '%s'", task.name, agent.name)
        try:
            result = agent.execute(task)
            return result
        except Exception as e:
            logger.exception("A2A: agent '%s' raised exception on task '%s'", agent.name, task.name)
            return A2AResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                error=A2AError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=str(e)[:500],
                    source_agent=agent.name,
                    source_task=task.id,
                ),
            )


# ═══════════════════════════════════════════════════════════════════
# Adapter helpers — JSON serialization for wire transfer
# ═══════════════════════════════════════════════════════════════════


def task_to_json(task: A2ATask) -> str:
    """Serialize an A2ATask to JSON for transmission."""
    return json.dumps(task.to_dict(), default=str)


def task_from_json(data: str | dict) -> A2ATask:
    """Deserialize an A2ATask from JSON."""
    if isinstance(data, str):
        data = json.loads(data)
    return A2ATask(
        id=data.get("id", ""),
        name=data.get("name", ""),
        input=data.get("input", {}),
        required_capabilities=data.get("required_capabilities", []),
        timeout_seconds=data.get("timeout_seconds", 120.0),
        metadata=data.get("metadata", {}),
        status=TaskStatus(data.get("status", "pending")),
        output=data.get("output", {}),
        error=_error_from_dict(data.get("error")),
        completed_at=data.get("completed_at", ""),
    )


def _error_from_dict(d: dict | None) -> A2AError | None:
    if not d:
        return None
    return A2AError(
        code=ErrorCode(d.get("code", "unknown")),
        message=d.get("message", ""),
        source_agent=d.get("source_agent", ""),
        source_task=d.get("source_task", ""),
        cause=_error_from_dict(d.get("cause")),
    )


def result_from_dict(data: dict) -> A2AResult:
    """Deserialize an A2AResult from a dict (JSON response)."""
    return A2AResult(
        task_id=data.get("task_id", ""),
        status=TaskStatus(data.get("status", "failed")),
        output=data.get("output", {}),
        error=_error_from_dict(data.get("error")),
        child_results=[result_from_dict(cr) for cr in data.get("child_results", [])],
        duration_ms=data.get("duration_ms", 0.0),
    )


# ═══════════════════════════════════════════════════════════════════
# LangChain adapter
# ═══════════════════════════════════════════════════════════════════

try:
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, Field

    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False
    BaseTool = None  # type: ignore
    BaseModel = None  # type: ignore
    Field = None  # type: ignore


def create_langchain_tool(delegator: A2ADelegator) -> Any:
    """Create a LangChain-compatible tool that delegates tasks via A2A.

    Usage:
        delegator = A2ADelegator()
        delegator.register(MyAgent())
        tool = create_langchain_tool(delegator)
        # Use with LangChain: agent.run(tool)
    """
    if not _LANGCHAIN_AVAILABLE:
        raise ImportError("langchain-core is required for LangChain adapter. pip install langchain-core")

    class A2ADelegationInput(BaseModel):
        task_name: str = Field(description="Name of the task to delegate")
        task_input: str = Field(description="JSON string of task input")
        required_capabilities: str = Field(
            description="Comma-separated list of required agent capabilities",
        )

    class A2ADelegationTool(BaseTool):  # type: ignore
        name: str = "a2a_delegate"
        description: str = "Delegate a task to a specialized sub-agent and return the result"
        args_schema: type[BaseModel] = A2ADelegationInput

        def _run(self, task_name: str, task_input: str, required_capabilities: str) -> str:
            import json as _json
            task = A2ATask(
                name=task_name,
                input=_json.loads(task_input),
                required_capabilities=[c.strip() for c in required_capabilities.split(",") if c.strip()],
            )
            result = delegator.delegate(task)
            return _json.dumps(result.to_dict(), default=str)

        async def _arun(self, task_name: str, task_input: str, required_capabilities: str) -> str:
            return self._run(task_name, task_input, required_capabilities)

    return A2ADelegationTool()