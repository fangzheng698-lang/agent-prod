"""@agent_gate decorator — 1-line integration for Python agents.

Usage
-----
    from agent_prod.integration import agent_gate

    @agent_gate(agent_type="my-agent", endpoint="http://localhost:8000")
    def run_agent(task: str) -> str:
        # ... existing agent logic ...
        return result

The decorator:
  1. Replaces ``openai.base_url`` → ``<endpoint>/v1/proxy`` (transparent proxy).
  2. Wraps the function to capture input/output and timing.
  3. After the function returns, submits an AgentTrace to the gate pipeline.
  4. Returns ``(result, gate_report)`` — the original result plus gate results.

For agents that do NOT use OpenAI SDK (raw httpx, Anthropic SDK, etc.):
  Set ``OPENAI_BASE_URL`` / ``ANTHROPIC_BASE_URL`` to the proxy endpoint manually.

See ``INTEGRATION.md`` for full documentation.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
import uuid

logger = logging.getLogger("agent_prod.integration")


def agent_gate(
    agent_type: str = "generic",
    endpoint: str = "http://localhost:8000",
    version: str = "",
    declared_tools: list[str] | None = None,
    proxy_llm: bool = True,
    auto_submit: bool = True,
    raise_on_reject: bool = False,
):
    """Decorate an agent's ``run()`` function to add quality gates.

    Parameters
    ----------
    agent_type:
        Unique identifier for your agent type. Used for per-agent thresholds
        and adapter routing.  Examples: ``"my-agent"``, ``"customer-support-v2"``.
    endpoint:
        URL of the agent-prod server.  Default ``http://localhost:8000``.
        In production you would point this at your deployed agent-prod instance.
    version:
        Version string for your agent (e.g. ``"1.2.0"``).  Included in every
        trace for tracking and regression baselines.
    declared_tools:
        List of tool names your agent may call.  Used by Gate0 for permission
        checks.  Example: ``["search", "calculator", "db_query"]``.
    proxy_llm:
        If True (default), set ``OPENAI_BASE_URL`` to the proxy endpoint so all
        LLM calls are automatically intercepted.  Disable if you handle
        interception yourself.
    auto_submit:
        If True (default), automatically submit a trace to the gate pipeline
        when the decorated function returns.  Set to False if you need to
        submit manually (e.g. for streaming/long-running agents).
    raise_on_reject:
        If True, raise ``GateRejectedError`` when the pipeline rejects the
        trace.  If False (default), the gate result is returned alongside the
        agent output as a tuple.

    Returns
    -------
    If the gate pipeline passes: ``(result, gate_report)`` where ``result`` is
    whatever the wrapped function returned and ``gate_report`` is a dict with
    ``passed=True`` and per-gate details.

    If the gate pipeline rejects and ``raise_on_reject=True``:
    ``GateRejectedError`` is raised.

    Example
    -------
    >>> @agent_gate(agent_type="my-agent", declared_tools=["search"])
    >>> def my_agent(query: str) -> str:
    ...     return "42"

    >>> result, gate = my_agent("what is the meaning?")
    >>> gate["passed"]
    True
    >>> gate["gates"][0]["gate"]
    'gate0'
    """
    if declared_tools is None:
        declared_tools = []

    def decorator(func):
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            return _run_with_gate(
                func, args, kwargs, agent_type, endpoint, version,
                declared_tools, proxy_llm, auto_submit, raise_on_reject,
            )

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            return await _run_with_gate_async(
                func, args, kwargs, agent_type, endpoint, version,
                declared_tools, proxy_llm, auto_submit, raise_on_reject,
            )

        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def _setup_proxy(endpoint: str) -> str:
    """Set env var so the agent's LLM SDK sends requests through agent-prod."""
    proxy_url = f"{endpoint.rstrip('/')}/v1/proxy"
    os.environ.setdefault("OPENAI_BASE_URL", proxy_url)
    os.environ.setdefault("ANTHROPIC_BASE_URL", proxy_url)
    return proxy_url


def _build_trace(
    agent_type: str,
    version: str,
    session_id: str,
    func_name: str,
    args: tuple,
    kwargs: dict,
    result,
    duration_ms: float,
    declared_tools: list[str] | None = None,
) -> dict:
    """Build an AgentTrace-compatible dict from function call metadata."""
    input_summary = _summarize(args, kwargs)
    output_summary = _summarize_value(result)

    trace = {
        "agent": agent_type,
        "version": version or f"{func_name}@unknown",
        "session_id": session_id,
        "output": {
            "final_response": str(output_summary)[:5000],
            "tools_used": declared_tools or [],
        },
        "decisions": [
            {
                "decision_id": f"{session_id}-turn-1",
                "model": os.environ.get("OPENAI_MODEL", "unknown"),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "tool_calls": [],
            }
        ],
        "current_metrics": {
            "latency_p95_ms": duration_ms,
            "success_rate": 1.0 if result is not None else 0.0,
            "error_rate": 0.0,
            "custom": {
                "function": func_name,
                "input_summary": str(input_summary)[:200],
            },
        },
        "declared_tools": declared_tools or [],
        "metadata": {
            "source": "agent_gate_decorator",
            "total_duration_ms": duration_ms,
        },
    }

    # Include exception info if result is an exception
    if isinstance(result, BaseException):
        trace["current_metrics"]["success_rate"] = 0.0
        trace["current_metrics"]["error_rate"] = 1.0
        trace["output"]["error"] = str(result)

    return trace


def _submit_trace(endpoint: str, trace: dict) -> dict:
    """POST an AgentTrace to the gate pipeline and return the result."""
    import httpx
    url = f"{endpoint.rstrip('/')}/v1/agent/evaluate"
    try:
        resp = httpx.post(url, json=trace, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {
            "passed": False,
            "error": f"Failed to submit trace: {e}",
            "status": "gate_error",
        }


def _summarize(args, kwargs) -> str:
    parts = []
    for a in args:
        parts.append(_summarize_value(a))
    for k, v in kwargs.items():
        parts.append(f"{k}={_summarize_value(v)}")
    return ", ".join(parts)


def _summarize_value(v) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    if len(s) > 200:
        s = s[:200] + "..."
    return s


def _run_with_gate(
    func, args, kwargs, agent_type, endpoint, version,
    declared_tools, proxy_llm, auto_submit, raise_on_reject,
):
    """Synchronous execution with gate evaluation."""
    session_id = f"deco_{uuid.uuid4().hex[:12]}"

    if proxy_llm:
        _setup_proxy(endpoint)

    start = time.monotonic()
    try:
        result = func(*args, **kwargs)
        duration_ms = (time.monotonic() - start) * 1000
    except Exception as e:
        duration_ms = (time.monotonic() - start) * 1000
        result = e

    if not auto_submit:
        return result

    trace = _build_trace(
        agent_type, version, session_id, func.__name__,
        args, kwargs, result, duration_ms, declared_tools,
    )

    gate_result = _submit_trace(endpoint, trace)
    passed = gate_result.get("passed", False)

    if raise_on_reject and not passed:
        raise GateRejectedError(
            agent_type=agent_type,
            session_id=session_id,
            gate_result=gate_result,
        )

    return result, gate_result


async def _run_with_gate_async(
    func, args, kwargs, agent_type, endpoint, version,
    declared_tools, proxy_llm, auto_submit, raise_on_reject,
):
    """Asynchronous execution with gate evaluation."""
    import httpx
    session_id = f"deco_{uuid.uuid4().hex[:12]}"

    if proxy_llm:
        _setup_proxy(endpoint)

    start = time.monotonic()
    try:
        result = await func(*args, **kwargs)
        duration_ms = (time.monotonic() - start) * 1000
    except Exception as e:
        duration_ms = (time.monotonic() - start) * 1000
        result = e

    if not auto_submit:
        return result

    trace = _build_trace(
        agent_type, version, session_id, func.__name__,
        args, kwargs, result, duration_ms, declared_tools,
    )

    # Asynchronous HTTP call
    url = f"{endpoint.rstrip('/')}/v1/agent/evaluate"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=trace)
            resp.raise_for_status()
            gate_result = resp.json()
    except Exception as e:
        gate_result = {
            "passed": False,
            "error": f"Failed to submit trace: {e}",
            "status": "gate_error",
        }

    passed = gate_result.get("passed", False)
    if raise_on_reject and not passed:
        raise GateRejectedError(
            agent_type=agent_type,
            session_id=session_id,
            gate_result=gate_result,
        )

    return result, gate_result


class GateRejectedError(Exception):
    """Raised when the quality gate pipeline rejects an agent trace.

    Attributes
    ----------
    agent_type:
        The agent type that was rejected.
    session_id:
        The session ID of the rejected trace.
    gate_result:
        The full gate result dict (status, failed_at, fail_reason, gates).
    """

    def __init__(
        self,
        agent_type: str,
        session_id: str,
        gate_result: dict,
    ):
        self.agent_type = agent_type
        self.session_id = session_id
        self.gate_result = gate_result
        failed_at = gate_result.get("failed_at", "unknown")
        reason = gate_result.get("fail_reason", "No reason")
        super().__init__(
            f"Gate '{failed_at}' rejected agent '{agent_type}' "
            f"(session={session_id}): {reason}"
        )