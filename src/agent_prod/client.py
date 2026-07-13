"""
AgentProdClient — one-line integration for any external agent.

    pip install agent-prod

    from agent_prod import AgentProdClient

    client = AgentProdClient()

    # Construct a trace as a plain dict — no dataclass imports needed
    trace = {
        "agent": "my-agent",
        "session_id": "ses_001",
        "decisions": [{
            "decision_id": "d1",
            "model": "gpt-4",
            "prompt_tokens": 500,
            "completion_tokens": 200,
            "tool_calls": [{"tool_id": "t1", "tool_name": "search", "success": True}],
        }],
    }

    # Dry-run: validate structure without executing gates
    result = client.dry_run(trace)
    if not result["valid"]:
        print("Fix:", result["errors"])

    # Evaluate: run all 5 gates
    result = client.evaluate(trace)
    if result["passed"]:
        print("✓ Deploy to production")
    else:
        print(f"✗ Blocked at {result['failed_at']}: {result['fail_reason']}")

    # Query thresholds
    thresholds = client.thresholds(agent="my-agent")

Three levels of trace construction:
  1. Plain dict  — just build a dict, no imports needed
  2. Helper     — `to_agent_trace(agent="my-agent", ...)` fills defaults
  3. Dataclass  — `AgentTrace(agent=AgentType.HERMES, ...)` full control
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

# ── Helper: construct AgentTrace dict with smart defaults ─────────

def to_agent_trace(
    *,
    agent: str,
    session_id: str = "",
    version: str = "",
    decisions: list[dict] | None = None,
    output: dict | None = None,
    baseline_metrics: dict | None = None,
    current_metrics: dict | None = None,
    traffic: dict | None = None,
    human_approver: str = "",
    policy_tags: list[str] | None = None,
    budget_tokens: int = 100_000,
    budget_time_ms: int = 120_000,
    **metadata,
) -> dict[str, Any]:
    """

    Construct an AgentTrace dict with sensible defaults.

    The caller provides only what they have. Missing fields get safe
    defaults so the trace is always valid for evaluation.

    Minimal example:

        trace = to_agent_trace(
            agent="claude-code",
            session_id="session-123",
            decisions=[{
                "decision_id": "d1",
                "model": "claude-sonnet-4",
                "tool_calls": [{"tool_id": "t1", "tool_name": "read_file"}],
            }],
        )
    """
    import uuid

    decisions = decisions or []
    output = output or {"final_response": ""}

    # Build MetricsSnapshot from simple key-value pairs
    if current_metrics:
        cm = {
            "latency_p95_ms": current_metrics.get("latency_p95_ms", 0.0),
            "success_rate": current_metrics.get("success_rate", 1.0),
            "error_rate": current_metrics.get("error_rate", 0.0),
            "token_efficiency": current_metrics.get("token_efficiency", 1.0),
        }
        custom = {
            k: v for k, v in current_metrics.items()
            if k not in cm
        }
        if custom:
            cm["custom"] = custom
    else:
        cm = {"success_rate": 1.0}

    bm = None
    if baseline_metrics:
        bm = {
            "latency_p95_ms": baseline_metrics.get("latency_p95_ms", 0.0),
            "success_rate": baseline_metrics.get("success_rate", 1.0),
            "error_rate": baseline_metrics.get("error_rate", 0.0),
            "token_efficiency": baseline_metrics.get("token_efficiency", 1.0),
        }
        bm_custom = {
            k: v for k, v in baseline_metrics.items()
            if k not in bm
        }
        if bm_custom:
            bm["custom"] = bm_custom

    return {
        "agent": agent,
        "version": version,
        "session_id": session_id or f"ses_{uuid.uuid4().hex[:12]}",
        "output": output,
        "decisions": decisions,
        "current_metrics": cm,
        "baseline_metrics": bm,
        "traffic": traffic,
        "human_approver": human_approver,
        "policy_tags": policy_tags or [],
        "budget_tokens": budget_tokens,
        "budget_time_ms": budget_time_ms,
        "metadata": metadata,
    }


# ── Client ────────────────────────────────────────────────────────

class AgentProdClient:
    """
    Synchronous HTTP client for agent-prod quality gates.

    Parameters:
        base_url:  Server URL (default from AGENT_PROD_URL env, else http://localhost:8000)
        timeout:   HTTP timeout in seconds per request
    """

    def __init__(self, base_url: str = "", timeout: float = 30):
        self.base_url = (
            base_url
            or os.environ.get("AGENT_PROD_URL", "")
            or "http://localhost:8001"
        ).rstrip("/")
        self.timeout = timeout

    # ── Health ──────────────────────────────────────────────

    def health(self) -> dict:
        """Check server health. Returns {"status": "ok", ...} or raises."""
        return self._get("/health")

    # ── Evaluate ────────────────────────────────────────────

    def evaluate(self, trace: dict | Any) -> dict:
        """
        Run all 5 quality gates on an agent trace.

        Returns:
            {"agent": "...", "session_id": "...", "status": "production"|"rejected",
             "passed": bool, "gates": [...], "failed_at": ..., "fail_reason": ...}

        Raises:
            AgentProdError: server unreachable or returned 4xx/5xx
        """
        payload = _to_dict(trace)
        return self._post("/v1/agent/evaluate", payload)

    # ── Dry-run ─────────────────────────────────────────────

    def dry_run(self, trace: dict | Any) -> dict:
        """
        Validate trace structure WITHOUT executing gates.

        Use this during integration to check that your trace format
        is correct before running full evaluation.

        Returns:
            {"valid": bool, "agent_type": ..., "adapter": ..., "errors": [...],
             "warnings": [...], "thresholds": {...}}

        The 'thresholds' field shows what per-agent thresholds WOULD
        be used if this trace were evaluated — helpful for debugging
        why a trace passes/fails.
        """
        payload = _to_dict(trace)
        return self._post("/v1/agent/evaluate/dry-run", payload)

    # ── Thresholds ──────────────────────────────────────────

    def thresholds(self, agent: str = "") -> dict:
        """
        Query current threshold configuration.

        Without agent: returns all agent types and their thresholds.
        With agent: returns thresholds specific to that agent type.

        agent="" → {"my-agent": {"gate3": {...}, "gate4": {...}}, ...}
        agent="my-agent" → {"gate3": {...}, "gate4": {...}}
        """
        params = f"?agent={agent}" if agent else ""
        return self._get(f"/v1/agent/thresholds{params}")

    # ── Agent types ─────────────────────────────────────────

    def agent_types(self) -> list[str]:
        """List supported agent types and their registered adapters."""
        resp = self._get("/v1/agent/types")
        return resp.get("agents", [])

    # ── Internal helpers ────────────────────────────────────

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise AgentProdError(
                f"GET {path}: HTTP {e.code}", code=e.code, body=body
            ) from e
        except urllib.error.URLError as e:
            raise AgentProdError(f"Cannot reach {self.base_url}: {e.reason}") from e

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        body_bytes = json.dumps(payload).encode()
        try:
            req = urllib.request.Request(
                url, data=body_bytes,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise AgentProdError(
                f"POST {path}: HTTP {e.code}", code=e.code, body=body
            ) from e
        except urllib.error.URLError as e:
            raise AgentProdError(f"Cannot reach {self.base_url}: {e.reason}") from e


# ── Error type ────────────────────────────────────────────────────

class AgentProdError(Exception):
    """Raised when the client cannot reach the server or gets an error."""

    def __init__(self, message: str, code: int = 0, body: str = ""):
        super().__init__(message)
        self.code = code
        self.body = body


# ── Dict converter ────────────────────────────────────────────────

def _to_dict(obj: Any) -> dict:
    """Convert AgentTrace dataclass or any compatible object to dict."""
    if isinstance(obj, dict):
        return obj
    # Handle dataclass
    if hasattr(obj, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(obj)
    # Handle Pydantic
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    raise AgentProdError(f"Cannot convert {type(obj).__name__} to dict")
