# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""QClaw Session Watchdog — auto-monitor qclaw sessions for quality gate evaluation.

Monitors ~/.qclaw/agents/main/sessions/ for new qclaw .jsonl session files.
When a new session file appears (i.e. a conversation ended), parses it and
submits the trace to agent-prod's gate pipeline.

Two modes:
  1. Direct file-based:  polls session directory, parses new files, POSTs to
     /v1/agent/evaluate (standalone, no agent-prod server needed for parsing,
     but needs server for evaluation).
  2. Via proxy: works best when qclaw's LLM traffic is routed through
     agent-prod's /v1/proxy endpoint (see INTEGRATION.md).

Usage:
    python -m agent_prod.integration.qclaw_watchdog
    # or programmatically:
    from agent_prod.integration.qclaw_watchdog import QClawWatchdog
    wd = QClawWatchdog()
    wd.start()
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_prod.integration.qclaw_parser import (
    is_subagent_tool,
    list_qclaw_sessions,
    parse_qclaw_session,
)

logger = logging.getLogger("agent_prod.qclaw_watchdog")

QCLAW_SESSIONS_DIR = Path.home() / ".qclaw" / "agents" / "main" / "sessions"
DEFAULT_AGENT_PROD_URL = "http://localhost:8000"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class QClawWatchdog:
    """Polling-based watcher for qclaw session files.

    Tracks .jsonl files in the qclaw sessions directory. When a new
    file appears, parses it and submits the trace to the quality gate pipeline.
    """

    def __init__(
        self,
        sessions_dir: Path = QCLAW_SESSIONS_DIR,
        agent_prod_url: str = DEFAULT_AGENT_PROD_URL,
        api_key: str | None = None,
        poll_interval: float = 5.0,
        agent_type: str = "qclaw",
        auto_approve_missing_human_approver: bool | None = None,
        auto_approver: str = "qclaw-auto",
    ):
        self.sessions_dir = sessions_dir
        self.agent_prod_url = agent_prod_url.rstrip("/")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.agent_type = agent_type
        self.auto_approve_missing_human_approver = (
            _env_flag("AGENT_PROD_QCLAW_AUTO_APPROVE")
            if auto_approve_missing_human_approver is None
            else auto_approve_missing_human_approver
        )
        self.auto_approver = auto_approver
        self._known: set[str] = set()
        self._running = False
        self._submitted_count = 0
        self._error_count = 0

    # ── Public API ────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog loop. Blocks until SIGINT/SIGTERM."""
        self._running = True

        if not self.sessions_dir.exists():
            logger.error("qclaw sessions directory not found: %s", self.sessions_dir)
            return

        # Seed known files (don't re-submit existing sessions on restart)
        for fpath in list_qclaw_sessions(str(self.sessions_dir)):
            self._known.add(fpath.name)

        logger.info(
            "QClaw watchdog started — monitoring %s (%d existing sessions)",
            self.sessions_dir,
            len(self._known),
        )
        logger.info("Target: %s/v1/agent/evaluate", self.agent_prod_url)

        try:
            while self._running:
                self._poll()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            logger.info(
                "QClaw watchdog stopped — %d submitted, %d errors",
                self._submitted_count,
                self._error_count,
            )

    def stop(self) -> None:
        """Signal the watchdog loop to stop."""
        self._running = False

    def poll_once(self) -> int:
        """Run a single poll cycle. Returns number of new sessions found."""
        return self._poll()

    # ── Internal ──────────────────────────────────────────

    def _poll(self) -> int:
        """Scan for new qclaw session files."""
        if not self.sessions_dir.exists():
            return 0

        new_count = 0
        for fpath in list_qclaw_sessions(str(self.sessions_dir)):
            if fpath.name in self._known:
                continue
            self._known.add(fpath.name)
            self._process(fpath)
            new_count += 1

        return new_count

    def _process(self, fpath: Path) -> None:
        """Parse a qclaw session file and submit to quality gate pipeline."""
        try:
            trace_data = parse_qclaw_session(
                fpath,
                agent_type=self.agent_type,
                source="qclaw_watchdog",
            )
            if trace_data is None:
                logger.warning("Failed to parse qclaw session: %s", fpath.name)
                self._error_count += 1
                return

            if (
                self.auto_approve_missing_human_approver
                and not trace_data.get("human_approver")
            ):
                trace_data["human_approver"] = self.auto_approver

            result = self._submit_evaluate(trace_data)

            self._submitted_count += 1
            status = result.get("status", "unknown")
            passed = result.get("passed", False)
            session_id = trace_data.get("session_id", "?")[:20]

            # ── 多智能体信息 ──
            output = trace_data.get("output", {})
            spawned = output.get("spawned_agents", [])
            subagent_tree = output.get("subagent_tree", {})
            meta = trace_data.get("metadata", {})
            n_subagents = meta.get("subagent_count", 0)

            # Count subagent tool calls vs normal tool calls
            all_tools = []
            for d in trace_data.get("decisions", []):
                for tc in d.get("tool_calls", []):
                    all_tools.append(tc.get("tool_name", ""))

            n_agent_tools = sum(1 for t in all_tools if not is_subagent_tool(t))
            n_subagent_ops = sum(1 for t in all_tools if is_subagent_tool(t))

            # Build rich log message
            parts = [f"qclaw session {session_id} → {status} (passed={passed})"]
            if n_subagents > 0:
                parts.append(f" subagents={n_subagents}")
            parts.append(f" tools={n_agent_tools}")
            if n_subagent_ops > 0:
                parts.append(f" agent-ops={n_subagent_ops}")
            if spawned:
                parts.append(f" tasks=[{', '.join(spawned[:5])}]")
                if len(spawned) > 5:
                    parts.append(f"... +{len(spawned)-5} more")

            logger.info(" ".join(parts))

            # If session had subagents, submit child traces
            if n_subagents > 0 and subagent_tree.get("children"):
                self._track_child_sessions(subagent_tree["children"],
                                           parent_trace=trace_data)

        except Exception as e:
            self._error_count += 1
            logger.warning("Failed to process qclaw session %s: %s", fpath.name, e)

    def _track_child_sessions(self, children: list[dict],
                              parent_trace: dict[str, Any] | None = None) -> None:
        """Submit child session traces to quality gate pipeline.

        qclaw does not store subagent conversations in separate .jsonl
        files — all child data is embedded in the parent session's JSONL
        (via sessions_spawn/sessions_yield/subagents events). So we
        construct lightweight traces from the subagent_tree metadata
        that was already parsed from the parent session.

        Each subagent gets evaluated independently through the gate
        pipeline, using its task_name and the parent's final response
        as context.
        """
        if not parent_trace:
            return

        parent_response = (
            parent_trace.get("output", {})
            .get("final_response", "")
        )
        spawned_names = (
            parent_trace.get("output", {})
            .get("spawned_agents", [])
        )

        submitted = 0
        for child in children:
            child_key = child.get("child_key", "")
            task_name = child.get("task_name", "unknown")
            if not child_key:
                continue

            # Build a descriptive task summary from the spawned_agents list
            task_desc = task_name
            for sa in spawned_names:
                if task_name in sa:
                    task_desc = sa
                    break

            # Rate limit: 0.3s pause between each submission
            if submitted > 0:
                time.sleep(0.3)

            child_trace = {
                "agent": f"{self.agent_type}_subagent",
                "version": parent_trace.get("version", "unknown"),
                "session_id": f"{child_key}-eval",
                "output": {
                    "final_response": (
                        f"Subagent '{task_name}' completed as part of parent session. "
                        f"Task: {task_desc}. "
                        f"Parent outcome: {parent_response[:2000]}"
                    )[:5000],
                    "tools_used": [],
                    "spawned_agents": [],
                },
                "decisions": [
                    {
                        "decision_id": f"{task_name}-result",
                        "model": parent_trace.get("version", "unknown"),
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning": "",
                        "tool_calls": [
                            {
                                "tool_id": f"{task_name}-spawn",
                                "tool_name": "sessions_spawn",
                                "arguments": {"task": task_name},
                                "result_summary": (
                                    f"Subagent '{task_name}' spawned with key {child_key[:20]}"
                                ),
                                "success": child.get("status") == "accepted",
                                "duration_ms": 0,
                            },
                        ],
                    },
                ],
                "declared_tools": ["sessions_spawn"],
                "current_metrics": {
                    "latency_p95_ms": 0,
                    "success_rate": 1.0,
                    "error_rate": 0.0,
                    "token_efficiency": 1.0,
                    "custom": {
                        "subagent_task": task_name,
                        "subagent_key": child_key,
                        "source": "qclaw_watchdog_child",
                    },
                },
                "metadata": {
                    "source": "qclaw_watchdog_child",
                    "parent_session": parent_trace.get("session_id", ""),
                    "subagent_task": task_name,
                    "child_key": child_key,
                    "run_id": child.get("run_id", ""),
                    "agent_type": self.agent_type,
                },
            }

            try:
                child_result = self._submit_evaluate(child_trace)
                self._submitted_count += 1
                submitted += 1
                child_status = child_result.get("status", "unknown")
                child_passed = child_result.get("passed", False)
                logger.info(
                    "  subagent %s (%s) → %s (passed=%s)",
                    child_key.split(":")[-1][:12] if ":" in child_key else child_key[:12],
                    task_name, child_status, child_passed,
                )
            except Exception as e:
                self._error_count += 1
                logger.warning("Failed to submit subagent %s: %s", task_name, e)

        if submitted:
            logger.info(
                "Submitted %d subagent traces from parent session %s",
                submitted, parent_trace.get("session_id", "?")[:20],
            )

    def _submit_evaluate(self, trace_data: dict[str, Any], _retries: int = 3) -> dict[str, Any]:
        """POST trace to /v1/agent/evaluate and return gate result.

        Retries up to 3 times on HTTP 429 (rate limit) with exponential backoff.
        """
        url = f"{self.agent_prod_url}/v1/agent/evaluate"
        data = json.dumps(trace_data).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        for attempt in range(_retries):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code == 429 and attempt < _retries - 1:
                    backoff = 1.5 ** (attempt + 1)
                    logger.debug("Rate limited, retrying in %.1fs (attempt %d/%d)", backoff, attempt + 1, _retries)
                    time.sleep(backoff)
                    continue
                logger.warning("Submit failed: HTTP %d — %s", e.code, body[:200])
                return {"status": "error", "error": f"HTTP {e.code}", "passed": False}
            except urllib.error.URLError as e:
                logger.warning("Submit failed: %s", e.reason)
                return {"status": "error", "error": str(e.reason), "passed": False}

        return {"status": "error", "error": "max retries exhausted", "passed": False}


# ═══════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════


def main():
    """Entry point: python -m agent_prod.integration.qclaw_watchdog"""
    import argparse

    parser = argparse.ArgumentParser(
        description="QClaw session watchdog for agent-prod",
    )
    parser.add_argument(
        "--sessions-dir",
        default=str(QCLAW_SESSIONS_DIR),
        help="qclaw sessions directory",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENT_PROD_URL", DEFAULT_AGENT_PROD_URL),
        help="agent-prod server URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AGENT_PROD_API_KEY", ""),
        help="API key for auth",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Poll interval in seconds",
    )
    parser.add_argument(
        "--agent-type",
        default="qclaw",
        help="Agent type identifier for gate thresholds",
    )
    parser.add_argument(
        "--auto-approve-missing-human-approver",
        action="store_true",
        default=_env_flag("AGENT_PROD_QCLAW_AUTO_APPROVE"),
        help=(
            "Fill missing human_approver with --auto-approver. Intended for local "
            "developer sessions; disabled by default."
        ),
    )
    parser.add_argument(
        "--auto-approver",
        default=os.environ.get("AGENT_PROD_QCLAW_AUTO_APPROVER", "qclaw-auto"),
        help="Approver label used when auto approval is enabled",
    )

    args = parser.parse_args()

    watchdog = QClawWatchdog(
        sessions_dir=Path(args.sessions_dir),
        agent_prod_url=args.url,
        api_key=args.api_key or None,
        poll_interval=args.interval,
        agent_type=args.agent_type,
        auto_approve_missing_human_approver=args.auto_approve_missing_human_approver,
        auto_approver=args.auto_approver,
    )

    signal.signal(signal.SIGTERM, lambda sig, frame: watchdog.stop())
    signal.signal(signal.SIGINT, lambda sig, frame: watchdog.stop())

    watchdog.start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
