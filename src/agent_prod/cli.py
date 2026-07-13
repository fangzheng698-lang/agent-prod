# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

#!/usr/bin/env python3
"""
agent-prod CLI — manage quality gate thresholds, watchdog, and server.

    agent-prod serve                              # start the server
    agent-prod init                               # interactive setup wizard
    agent-prod watch                              # start session watchdog
    agent-prod configure                          # configuration wizard
    agent-prod configure --show                   # show current config
    agent-prod configure --reset                  # reset to defaults
    agent-prod stats                              # evaluation statistics
    agent-prod stats --agent qclaw                # filter by agent
    agent-prod stats --detail <id>                # single eval detail
    agent-prod feedback                           # flywheel improvements
    agent-prod feedback --id <id>                 # single improvement detail
    agent-prod feedback --apply <id>              # apply improvement
    agent-prod show thresholds                    # show all thresholds
    agent-prod show thresholds --agent hermes     # show hermes only
    agent-prod set threshold gate3 hermes regress_pct 0.92
    agent-prod doctor                             # health + security status
    agent-prod migrate                            # create DB tables

The CLI reads/writes `gates/config.yaml` directly. For live config,
it also queries the running server (AGENT_PROD_URL or localhost:8000).

Usage:
    agent-prod <command> [<args>...]
    agent-prod --help
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

import yaml

from agent_prod.cli_common import CONFIG_PATH, load_config as _load_config, save_config as _save_config, live_server as _live_server

# Project root — walk up from this file
PROJECT_ROOT = Path(__file__).resolve().parent


# ── Commands ──────────────────────────────────────────────────────


def cmd_migrate(args: argparse.Namespace) -> None:
    """Create database tables for supported backends."""
    import sqlite3
    config = _load_config()
    repo_cfg = config.get("repository", {})
    backend = repo_cfg.get("backend", "file")

    if backend == "postgres":
        dsn = repo_cfg.get("dsn", "postgresql://user:***@localhost:5432/quality_gates")
        print(f"Postgres DSN: {dsn}")
        print("Run manually:")
        print("""
CREATE TABLE IF NOT EXISTS improvements (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_improvements_status ON improvements(status);
CREATE INDEX IF NOT EXISTS idx_improvements_created_at ON improvements(created_at);
""")
    elif backend == "sqlite":
        db_path = repo_cfg.get("path", "/var/lib/quality_gates/improvements.db")
        print(f"Creating SQLite DB at {db_path}...")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
CREATE TABLE IF NOT EXISTS improvements (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON improvements(status)")
        conn.commit()
        conn.close()
        print("SQLite tables created OK")
    else:
        file_path = config.get("repository", {}).get("file_path", "/var/lib/quality_gates/improvements.json")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(file_path).exists():
            Path(file_path).write_text("{}")
        print(f"FileRepository ready: {file_path}")


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the agent-prod FastAPI server."""
    import uvicorn
    host = args.host or os.environ.get("AGENT_PROD_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("AGENT_PROD_PORT", "8000"))
    print(f"🚀 Starting agent-prod server on {host}:{port}")

    # Auto-start Hermes session watchdog (zero-touch integration)
    if not args.no_watchdog:
        from agent_prod.server.app import _start_watchdog_thread
        _start_watchdog_thread(port)

    uvicorn.run("agent_prod.server.app:app", host=host, port=port, reload=args.reload)


def cmd_init(args: argparse.Namespace) -> None:
    """交互式初始化向导 — 一键配置 LLM + Agent 模式 + 启动服务。"""
    from agent_prod.cli_init import cmd_init as _new_init
    _new_init(args)


def cmd_watch(args: argparse.Namespace) -> None:
    """Start the Hermes session watchdog."""
    from agent_prod.ingest.watchdog import SessionWatchdog

    sessions_dir = Path(args.sessions_dir) if args.sessions_dir else Path.home() / ".hermes" / "sessions"
    url = args.url or os.environ.get("AGENT_PROD_URL", "http://localhost:8000")
    api_key = args.api_key or os.environ.get("AGENT_PROD_API_KEY", "")

    watchdog = SessionWatchdog(
        sessions_dir=sessions_dir,
        agent_prod_url=url,
        api_key=api_key or None,
        poll_interval=args.interval,
    )

    signal.signal(signal.SIGTERM, lambda sig, frame: watchdog.stop())
    signal.signal(signal.SIGINT, lambda sig, frame: watchdog.stop())

    watchdog.start()


def cmd_show(args: argparse.Namespace) -> None:
    """Show thresholds — from live server if available, else from config."""
    base = _live_server()

    # Try live server first
    try:
        import urllib.request
        url = f"{base}/v1/agent/thresholds"
        if args.agent:
            url += f"?agent={args.agent}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:
        # Fall back to local config
        config = _load_config()
        gates = config.get("gates", {})

        gate3 = gates.get("gate3", {})
        gate4 = gates.get("gate4", {})

        if args.agent:
            from agent_prod.gates.thresholds import resolve_agent_thresholds
            g3 = resolve_agent_thresholds("gate3", args.agent, config)
            g4 = resolve_agent_thresholds("gate4", args.agent, config)
            data = {"agent": args.agent, "gate3": g3, "gate4": g4}
        else:
            from agent_prod.gates.thresholds import resolve_agent_thresholds
            g3d = resolve_agent_thresholds("gate3", "", config)
            g4d = resolve_agent_thresholds("gate4", "", config)
            per_agents_g3 = gate3.get("per_agent", {})
            per_agents_g4 = gate4.get("per_agent", {})
            all_agents = sorted(set(per_agents_g3.keys()) | set(per_agents_g4.keys()))
            agents = {}
            for a in all_agents:
                agents[a] = {
                    "gate3": resolve_agent_thresholds("gate3", a, config),
                    "gate4": resolve_agent_thresholds("gate4", a, config),
                }
            data = {"_defaults": {"gate3": g3d, "gate4": g4d}, "agents": agents}

    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_set(args: argparse.Namespace) -> None:
    """Set a per-agent threshold value directly in config.yaml."""
    config = _load_config()
    gates = config.setdefault("gates", {})

    gate = gates.setdefault(args.gate, {})
    per_agent = gate.setdefault("per_agent", {})
    agent_cfg = per_agent.setdefault(args.agent, {})

    # Convert value
    try:
        val = float(args.value)
        if val == int(val):
            val = int(val)
    except ValueError:
        val = args.value

    agent_cfg[args.key] = val
    _save_config(config)

    print(f"Set {args.gate}.per_agent.{args.agent}.{args.key} = {val}")
    print("Restart the server or use 'agent-prod reload' to pick up the change.")


def cmd_install(args: argparse.Namespace) -> None:
    """Install agent-prod integration into Hermes.

    Copies the on_session_end plugin to Hermes's plugins directory,
    enabling event-driven quality gate evaluation (Path 2).
    """
    import shutil

    plugin_src = PROJECT_ROOT / "hermes_plugin"
    plugin_dst = Path.home() / ".hermes" / "hermes-agent" / "plugins" / "agent-prod"

    if not plugin_src.exists():
        print(f"❌ Plugin source not found: {plugin_src}")
        sys.exit(1)

    # Copy plugin files
    plugin_dst.mkdir(parents=True, exist_ok=True)
    for fname in ("plugin.yaml", "__init__.py"):
        shutil.copy2(plugin_src / fname, plugin_dst / fname)

    print(f"✅ Plugin installed: {plugin_dst}")
    print()
    print("Integration paths:")
    print(f"  Path 1 (Watchdog):  auto-started with `agent-prod serve`")
    print(f"  Path 2 (Hook):      Hermes plugin → on_session_end → evaluate")
    print()
    print("Test it:")
    print(f"  agent-prod serve")
    print(f"  # Then use Hermes normally. Each session end auto-evaluates.")
    print()
    print("Verify:")
    print(f"  curl http://localhost:8765/health  # check watchdog_active: true")
    print(f"  agent-prod doctor")


def cmd_doctor(args: argparse.Namespace) -> None:
    """Health check."""
    base = _live_server()
    try:
        import urllib.request
        req = urllib.request.Request(f"{base}/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  Server: UNAVAILABLE ({e})")
        sys.exit(1)

    print(f"  Status  : {data.get('status')}")
    print(f"  Model   : {data.get('model')}")
    print(f"  Gates   : {'ENABLED' if data.get('quality_gates') else 'DISABLED'}")
    print(f"  Auth    : {'ENABLED' if data.get('auth_enabled') else 'DISABLED'}")
    print(f"  RateLimit: {'ENABLED' if data.get('rate_limit_enabled') else 'DISABLED'}")
    print(f"  Watchdog : {'ACTIVE' if data.get('watchdog_active') else 'inactive'}")
    print(f"  Sessions: {data.get('sessions_active', 0)} active")

    # Check Hermes plugin installation
    plugin_dir = Path.home() / ".hermes" / "hermes-agent" / "plugins" / "agent-prod"
    if plugin_dir.exists() and (plugin_dir / "plugin.yaml").exists():
        print(f"  Plugin   : INSTALLED ({plugin_dir})")
    else:
        print(f"  Plugin   : not installed (run 'agent-prod install')")

    # Check thresholds
    try:
        req = urllib.request.Request(f"{base}/v1/agent/thresholds", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            thresholds = json.loads(resp.read())
        agents = list(thresholds.get("agents", {}).keys())
        print(f"  Agents  : {', '.join(agents) if agents else 'default only'}")
    except Exception:
        print("  Agents  : unknown")

    print(f"  Server  : {base}")


# ── Evaluate ────────────────────────────────────────────────────


def cmd_evaluate(args: argparse.Namespace) -> None:
    """Evaluate a trace from the CLI."""
    server = _live_server()
    import json, urllib.request

    payload = {
        "agent": args.agent,
        "session_id": args.session or f"cli_{int(time.time())}",
        "decisions": [{
            "decision_id": "cli-eval",
            "model": "cli",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tool_calls": [],
        }],
        "current_metrics": {
            "latency_p95_ms": args.latency,
            "success_rate": args.success_rate,
            "final_response": args.response,
        },
        "traffic_percentage": args.traffic,
        "human_approver": "cli",
        "metadata": {},
    }
    if args.expected_plan:
        payload["current_metrics"]["expected_plan"] = args.expected_plan
        payload["metadata"]["expected_plan"] = args.expected_plan
    if args.gate7_mode:
        payload["metadata"]["gate7_mode"] = args.gate7_mode

    url = f"{server}/v1/agent/evaluate"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Error: cannot reach server ({e})")
        sys.exit(1)

    # Display result
    passed = result.get("passed", False)
    status = result.get("status", "?")
    failed_at = result.get("failed_at", "")
    icon = "✅" if passed else "❌"
    print(f"\n{icon}  Result: {status}")
    if failed_at:
        print(f"   Failed at: {failed_at}")
        if result.get("fail_reason"):
            print(f"   Reason: {result['fail_reason']}")
    print(f"\n   Gate details:")
    for g in result.get("gates", []):
        gn = g.get("gate_name", g.get("gate", "?"))
        gp = g.get("passed", False)
        gi = "✅" if gp else "❌"
        gr = g.get("reason", "")[:80]
        print(f"     {gi} {gn}: {gr}")


# ── Logs ────────────────────────────────────────────────────────


def cmd_logs(args: argparse.Namespace) -> None:
    """View historical evaluation logs from the server."""
    server = _live_server()
    import json, urllib.request

    url = f"{server}/v1/agent/stats?limit={args.limit}"
    if args.agent:
        url += f"&agent={args.agent}"
    if args.status:
        url += f"&status={args.status}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Error: cannot reach server ({e})")
        sys.exit(1)

    recent = data.get("recent", data.get("evaluations", []))
    if not recent:
        print("No evaluations found.")
        return

    if args.json:
        print(json.dumps(recent, indent=2, ensure_ascii=False))
        return

    print(f"\n  Recent Evaluations (last {len(recent)})\n")
    print(f"  {'ID':<30s} {'Agent':<18s} {'Status':<12s} {'Gates Passed':<14s} {'Failed At'}")
    print(f"  {'─'*30:<30s} {'─'*18:<18s} {'─'*12:<12s} {'─'*14:<14s} {'─'*30}")
    for r in recent:
        rid = r.get("session_id", r.get("id", "?"))[:28]
        agent = r.get("agent", "?")[:16]
        status = r.get("status", "?")
        if str(status).lower() == "production":
            status_display = f"\033[92m{status}\033[0m"
        else:
            status_display = f"\033[91m{status}\033[0m"
        gates_passed = r.get("gates_passed", r.get("passed_gates", "?"))
        failed_at = r.get("failed_at", r.get("fail_gate", "")) or "—"
        print(f"  {rid:<30s} {agent:<18s} {status_display:<18s} {str(gates_passed):<14s} {str(failed_at)[:28]:<30s}")
    print()


# ── Alert ───────────────────────────────────────────────────────


def cmd_alert(args: argparse.Namespace) -> None:
    """Configure alerting for gate rejections."""


# ── Approval commands (Phase 3) ────────────────────────────────────

def cmd_approval_list(args: argparse.Namespace) -> None:
    """List approval records from the running server."""
    import urllib.request
    import urllib.error

    base = _live_server()
    qs = []
    if args.agent:
        qs.append(f"agent={args.agent}")
    if args.status:
        qs.append(f"status={args.status}")
    url = f"{base}/v1/approvals" + ("?" + "&".join(qs) if qs else "")

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Error: cannot reach server ({e})")
        sys.exit(1)

    records = payload.get("approvals", []) if isinstance(payload, dict) else payload
    if not records:
        print("No approval records found.")
        return

    print(f"Approval records ({len(records)}):")
    print()
    for r in records:
        status = r.get("status", "?")
        age = r.get("age_seconds")
        age_str = f"  age={int(age)}s" if age is not None else ""
        print(f"  [{status}] {r.get('approval_id', '?')}")
        print(f"    agent: {r.get('agent', '?')}  improvement: {r.get('improvement_id', '?')}")
        print(f"    gate: {r.get('gate', '?')}  requested: {r.get('requested_at', '?')}{age_str}")
        if r.get("decided_by"):
            print(f"    decided_by: {r.get('decided_by')}  reason: {r.get('decision_reason', '')}")
        print()


def cmd_approval_decide(args: argparse.Namespace, approved: bool) -> None:
    """Approve or reject a pending approval and resume the pipeline."""
    import urllib.request
    import urllib.error

    base = _live_server()
    url = f"{base}/v1/approvals/{args.approval_id}/decide"
    body = {
        "approved": approved,
        "approver": args.approver,
        "reason": args.reason or "",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body_text}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: cannot reach server ({e})")
        sys.exit(1)

    status = result.get("status", "?")
    approved_flag = result.get("approved", approved)
    improvement_id = result.get("improvement_id", "")
    verb = "Approved" if approved else "Rejected"
    icon = "✅" if (approved_flag and status == "production") else ("❌" if not approved else "⚠️")
    print(f"\n{icon}  {verb}: approval={args.approval_id} improvement={improvement_id}")
    print(f"    status={status}")


def cmd_approval_approve(args: argparse.Namespace) -> None:
    cmd_approval_decide(args, approved=True)


def cmd_approval_reject(args: argparse.Namespace) -> None:
    cmd_approval_decide(args, approved=False)


# ── Registry commands ──────────────────────────────────────────────

def cmd_registry_publish(args: argparse.Namespace) -> None:
    """Publish an MCP server to the registry."""
    from agent_prod.registry import publish_entry

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
    result = publish_entry(
        name=args.name,
        command=args.command,
        description=args.description,
        tags=tags,
        author=args.author,
        homepage=args.homepage,
        repository=args.repository,
        remote=args.remote,
    )
    print(f"✅ Published {args.name} to MCP registry")
    print(f"   Command: {args.command}")
    print(f"   Tags: {', '.join(tags) or '(none)'}")
    if args.remote and "remote" in result:
        print(f"   Remote ID: {result['remote'].get('id', 'unknown')}")


def cmd_registry_search(args: argparse.Namespace) -> None:
    """Search for MCP servers in the registry."""
    from agent_prod.registry import search_entries

    results = search_entries(args.query, local_only=not args.remote)
    if not results:
        print(f"No results found for '{args.query}'")
        return

    print(f"Found {len(results)} server(s) matching '{args.query}':")
    print()
    for r in results:
        tags = ", ".join(r.get("tags", []))[:60]
        print(f"  {r['name']}")
        print(f"    {r.get('description', '')[:80]}")
        print(f"    Command: {r.get('command', '')}")
        if tags:
            print(f"    Tags: {tags}")
        print()


def cmd_registry_list(args: argparse.Namespace) -> None:
    """List all locally registered MCP servers."""
    from agent_prod.registry import LocalRegistry

    registry = LocalRegistry()
    entries = registry.list_all()
    if not entries:
        print("No MCP servers registered locally.")
        print("Use 'agent-prod registry publish' to add one.")
        return

    print(f"Local MCP Registry ({registry.count()} server(s)):")
    print()
    for r in sorted(entries, key=lambda x: x["name"]):
        tags = ", ".join(r.get("tags", []))[:60]
        verified = " ✅" if r.get("verified") else ""
        print(f"  {r['name']}{verified}")
        print(f"    {r.get('description', '')[:80]}")
        print(f"    Command: {r.get('command', '')}")
        if tags:
            print(f"    Tags: {tags}")
        print()


# ── alert ──
    print("Alert configuration updated.")
    print("Restart the server to apply: agent-prod serve")


# ── Main ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    # Lazy imports to avoid circular deps with cli_*.py modules
    from agent_prod.cli_configure import cmd_configure
    from agent_prod.cli_stats import cmd_stats
    from agent_prod.cli_feedback import cmd_feedback
    import urllib.error

    parser = argparse.ArgumentParser(
        prog="agent-prod",
        description="Enterprise AI agent quality gate infrastructure.",
    )
    sub = parser.add_subparsers(dest="command")

    # ── serve ──
    serve_parser = sub.add_parser("serve", help="Start the agent-prod server")
    serve_parser.add_argument("--host", help="Bind host (default: 0.0.0.0)")
    serve_parser.add_argument("--port", type=int, help="Bind port (default: 8000)")
    serve_parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    serve_parser.add_argument("--no-watchdog", action="store_true",
                              help="Disable auto-start Hermes session watchdog")
    serve_parser.set_defaults(func=cmd_serve)

    # ── init ──
    init_parser = sub.add_parser("init", help="Interactive setup wizard")
    init_parser.set_defaults(func=cmd_init)

    # ── watch ──
    watch_parser = sub.add_parser("watch", help="Start Hermes session watchdog")
    watch_parser.add_argument("--sessions-dir", help="Hermes sessions directory")
    watch_parser.add_argument("--url", help="agent-prod server URL")
    watch_parser.add_argument("--api-key", help="API key for auth")
    watch_parser.add_argument("--interval", type=float, default=1.0, help="Poll interval (seconds)")
    watch_parser.set_defaults(func=cmd_watch)

    # ── show ──
    show_parser = sub.add_parser("show", help="Show thresholds")
    show_parser.add_argument("resource", choices=["thresholds"], help="What to show")
    show_parser.add_argument("--agent", help="Filter by agent type")
    show_parser.set_defaults(func=cmd_show)

    # ── set ──
    set_parser = sub.add_parser("set", help="Set a threshold value")
    set_parser.add_argument("resource", choices=["threshold"], help="Resource to set")
    set_parser.add_argument("gate", choices=["gate3", "gate4"], help="Gate name")
    set_parser.add_argument("agent", help="Agent type (e.g. hermes, claude-code)")
    set_parser.add_argument("key", help="Threshold key (e.g. regress_pct)")
    set_parser.add_argument("value", help="New value")
    set_parser.set_defaults(func=cmd_set)

    # ── doctor ──
    doctor_parser = sub.add_parser("doctor", help="Health check")
    doctor_parser.set_defaults(func=cmd_doctor)

    # ── install ──
    install_parser = sub.add_parser("install", help="Install agent-prod integration into Hermes")
    install_parser.set_defaults(func=cmd_install)

    # ── configure ──
    configure_parser = sub.add_parser("configure", help="Configuration wizard")
    configure_parser.add_argument("--show", action="store_true", help="Display current configuration")
    configure_parser.add_argument("--reset", action="store_true", help="Reset config to defaults")
    configure_parser.add_argument("--mode", choices=["observe", "enforce"],
                                  help="Set Gate0 mode for an agent (use with --agent)")
    configure_parser.add_argument("--gate7-mode", choices=["observe", "enforce"],
                                  help="Set Gate7 mode for an agent (use with --agent)")
    configure_parser.add_argument("--agent", help="Agent name for --mode / --gate7-mode")
    configure_parser.set_defaults(func=cmd_configure)

    # ── stats ──
    stats_parser = sub.add_parser("stats", help="Query evaluation statistics")
    stats_parser.add_argument("--agent", help="Filter by agent type (e.g. qclaw, claude-code)")
    stats_parser.add_argument("--rejected", action="store_true", help="Show only rejected evaluations")
    stats_parser.add_argument("--detail", help="Show single evaluation detail by ID")
    stats_parser.add_argument("--plan-report", action="store_true", help="Show plan vs execution consistency report")
    stats_parser.set_defaults(func=cmd_stats)

    # ── feedback ──
    feedback_parser = sub.add_parser("feedback", help="Query flywheel improvement suggestions")
    feedback_parser.add_argument("--id", help="Show single improvement detail by ID")
    feedback_parser.add_argument("--apply", help="Apply an improvement suggestion to config")
    feedback_parser.set_defaults(func=cmd_feedback)

    # ── evaluate ──
    evaluate_parser = sub.add_parser("evaluate", help="Evaluate a single agent trace from the command line")
    evaluate_parser.add_argument("--agent", default="cli-agent", help="Agent name")
    evaluate_parser.add_argument("--session", default="", help="Session ID (auto-generated if empty)")
    evaluate_parser.add_argument("--response", required=True, help="Final response text")
    evaluate_parser.add_argument("--expected-plan", default="", help="Expected plan (for Gate7 consistency check)")
    evaluate_parser.add_argument("--gate7-mode", choices=["observe", "enforce"], default="observe",
                                  help="Gate7 mode (default: observe)")
    evaluate_parser.add_argument("--traffic", type=int, default=1, help="Traffic percentage (default: 1)")
    evaluate_parser.add_argument("--latency", type=int, default=100, help="P95 latency in ms (default: 100)")
    evaluate_parser.add_argument("--success-rate", type=float, default=1.0, help="Success rate 0-1 (default: 1.0)")
    evaluate_parser.set_defaults(func=cmd_evaluate)

    # ── logs ──
    logs_parser = sub.add_parser("logs", help="View historical evaluation logs")
    logs_parser.add_argument("--agent", help="Filter by agent")
    logs_parser.add_argument("--status", choices=["production", "rejected"], help="Filter by status")
    logs_parser.add_argument("--limit", type=int, default=20, help="Number of logs to show (default: 20)")
    logs_parser.add_argument("--json", action="store_true", help="Output as JSON")
    logs_parser.set_defaults(func=cmd_logs)

    # ── alert ──
    alert_parser = sub.add_parser("alert", help="Configure alerting for gate rejections")
    alert_parser.add_argument("--enable", action="store_true", help="Enable alerts")
    alert_parser.add_argument("--disable", action="store_true", help="Disable alerts")
    alert_parser.add_argument("--show", action="store_true", help="Show current alert config")
    alert_parser.add_argument("--webhook", help="Set webhook URL for alerts")
    alert_parser.add_argument("--on", choices=["gate3", "gate6", "gate7", "any"], default="any",
                               help="Which gate rejections trigger alerts (default: any)")
    alert_parser.set_defaults(func=cmd_alert)

    # ── approval ──
    approval_parser = sub.add_parser("approval", help="Manage Gate5 pending approval requests")
    approval_sub = approval_parser.add_subparsers(dest="approval_command")

    appr_list = approval_sub.add_parser("list", help="List approval records")
    appr_list.add_argument("--agent", help="Filter by agent name")
    appr_list.add_argument("--status", choices=["pending", "approved", "rejected", "expired"],
                            default="pending", help="Filter by status (default: pending)")
    appr_list.set_defaults(func=cmd_approval_list)

    appr_approve = approval_sub.add_parser("approve", help="Approve a pending request and resume the pipeline")
    appr_approve.add_argument("approval_id", help="Approval ID")
    appr_approve.add_argument("--approver", required=True, help="Name of the approver")
    appr_approve.add_argument("--reason", default="", help="Optional reason")
    appr_approve.set_defaults(func=cmd_approval_approve)

    appr_reject = approval_sub.add_parser("reject", help="Reject a pending request")
    appr_reject.add_argument("approval_id", help="Approval ID")
    appr_reject.add_argument("--approver", required=True, help="Name of the approver")
    appr_reject.add_argument("--reason", default="", help="Optional reason")
    appr_reject.set_defaults(func=cmd_approval_reject)

    # ── registry ──
    registry_parser = sub.add_parser("registry", help="MCP server registry operations")
    registry_sub = registry_parser.add_subparsers(dest="registry_command")

    reg_publish = registry_sub.add_parser("publish", help="Publish an MCP server to the registry")
    reg_publish.add_argument("name", help="Server name")
    reg_publish.add_argument("--command", required=True, help="Command to run the server (e.g. 'uvx my-server')")
    reg_publish.add_argument("--description", required=True, help="Short description")
    reg_publish.add_argument("--tags", help="Comma-separated tags")
    reg_publish.add_argument("--author", default="", help="Author name")
    reg_publish.add_argument("--homepage", default="", help="Project homepage URL")
    reg_publish.add_argument("--repository", default="", help="Source repository URL")
    reg_publish.add_argument("--remote", action="store_true", help="Also publish to remote registry")
    reg_publish.set_defaults(func=cmd_registry_publish)

    reg_search = registry_sub.add_parser("search", help="Search MCP servers in the registry")
    reg_search.add_argument("query", help="Search query")
    reg_search.add_argument("--remote", action="store_true", help="Also search remote registry")
    reg_search.set_defaults(func=cmd_registry_search)

    reg_list = registry_sub.add_parser("list", help="List all local MCP servers")
    reg_list.set_defaults(func=cmd_registry_list)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
