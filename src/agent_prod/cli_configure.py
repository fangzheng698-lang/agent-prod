"""agent-prod configure — interactive setup wizard.

Usage:
    agent-prod configure              interactive wizard
    agent-prod configure --show       display current config
    agent-prod configure --reset      restore defaults

Exports:
    cmd_configure(args: argparse.Namespace) -> None
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from agent_prod.cli_common import CONFIG_PATH, load_config, save_config

KNOWN_AGENTS = ["hermes", "claude-code", "codex", "opencode", "qclaw"]
STORAGE_BACKENDS = ["memory", "production"]


# ── Helpers ────────────────────────────────────────────────────────


def _mask_value(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def _confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer[0] == "y"


def _prompt_str(prompt: str, default: str = "", password: bool = False) -> str:
    display_default = _mask_value(default) if password else default
    try:
        value = input(f"{prompt} [{display_default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not value:
        return default
    return value


def _prompt_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        try:
            value = input(f"{prompt} ({'/'.join(choices)}) [{default}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not value:
            return default
        if value in choices:
            return value
        print(f"  Invalid choice. Must be one of: {', '.join(choices)}")


# ── Display ────────────────────────────────────────────────────────


def _display_config(config: dict) -> None:
    """Pretty-print current configuration as tables."""
    gates = config.get("gates", {})
    security = config.get("security", {})
    storage = config.get("storage", {})
    tools_cfg = config.get("tools", {})

    print()
    print("agent-prod Configuration")
    print("=" * 60)

    # ── Gate6 (LLM) ──
    gate6 = gates.get("gate6", {})
    print()
    print("Gate6 (LLM Evaluation)")
    print("-" * 60)
    llm_endpoint = gate6.get("llm_endpoint", "(not configured)")
    llm_model = gate6.get("llm_model", "(not configured)")
    pass_threshold = gate6.get("pass_threshold", 0.58)
    print(f"  LLM Endpoint  .....  {llm_endpoint}")
    print(f"  LLM Model   .......  {llm_model}")
    print(f"  Pass Threshold  ...  {pass_threshold}")
    api_key = os.environ.get("OPENAI_API_KEY", "") or security.get("api_key", "")
    print(f"  API Key  ..........  {_mask_value(api_key)} (from env or config)")

    # ── Gate0 (Permission Mode) ──
    gate0 = gates.get("gate0", {})
    per_agent_modes = gate0.get("per_agent", {})
    global_mode = gate0.get("mode", "enforce")
    print()
    print("Gate0 (Permission Mode)")
    print("-" * 60)
    print(f"  {'Agent':<20} {'Mode':<10}")
    print(f"  {'-'*18:<20} {'-'*8:<10}")
    for agent in KNOWN_AGENTS:
        mode = per_agent_modes.get(agent, {}).get("mode", global_mode)
        print(f"  {agent:<20} {mode:<10}")
    # Show agents not in KNOWN_AGENTS
    for agent in sorted(per_agent_modes.keys()):
        if agent not in KNOWN_AGENTS:
            mode = per_agent_modes[agent].get("mode", global_mode)
            print(f"  {agent:<20} {mode:<10}")
    print(f"  (global default: {global_mode})")

    # ── Gate6 per-agent thresholds ──
    per_agent_thresholds = gate6.get("per_agent", {})
    print()
    print("Gate6 (Pass Threshold per Agent)")
    print("-" * 60)
    print(f"  {'Agent':<20} {'Threshold':<10}")
    print(f"  {'-'*18:<20} {'-'*8:<10}")
    all_agents = set(KNOWN_AGENTS) | set(per_agent_thresholds.keys())
    for agent in sorted(all_agents):
        th = per_agent_thresholds.get(agent, {}).get("pass_threshold", pass_threshold)
        print(f"  {agent:<20} {th:<10.2f}")

    # ── Storage ──
    print()
    print("Storage")
    print("-" * 60)
    backend = storage.get("backend", "memory")
    file_path = storage.get("file_path", "./data/improvements.json")
    print(f"  Backend  .........  {backend}")
    print(f"  File Path  .......  {file_path}")

    # ── Tools Aliases ──
    aliases = tools_cfg.get("aliases", {})
    print()
    print("Tools (Aliases per Agent)")
    print("-" * 60)
    print(f"  {'Agent':<20} {'Aliases':<10}")
    print(f"  {'-'*18:<20} {'-'*8:<10}")
    for agent in sorted(aliases.keys()):
        n = len(aliases[agent])
        print(f"  {agent:<20} {n:<10}")
    if not aliases:
        print("  (none configured)")

    # ── Observability ──
    obs = config.get("observability", {})
    otel = obs.get("otel", {})
    metrics = obs.get("metrics", {})
    if otel or metrics:
        print()
        print("Observability")
        print("-" * 60)
        if otel.get("enabled"):
            print(f"  OTel  ............  enabled ({otel.get('endpoint', 'default')})")
        else:
            print("  OTel  ............  disabled")
        print(f"  Metrics Provider    {metrics.get('provider', 'none')}")

    print()


# ── Interactive wizard ─────────────────────────────────────────────


def _interactive_configure(config: dict) -> dict:
    """Interactive configuration wizard. Returns modified config dict."""
    print()
    print("agent-prod Configuration Wizard")
    print("=" * 60)
    print("Press Enter to keep current value.\n")

    gates = config.setdefault("gates", {})
    security = config.setdefault("security", {})

    # 1. LLM Endpoint
    gate6 = gates.setdefault("gate6", {})
    current = gate6.get("llm_endpoint", "")
    val = _prompt_str("LLM API endpoint URL", default=current)
    if val and val.startswith(("http://", "https://")):
        gate6["llm_endpoint"] = val
    elif val:
        print("  Warning: URL should start with http:// or https://")

    # 2. LLM Model
    current = gate6.get("llm_model", "")
    val = _prompt_str("LLM model name", default=current)
    if val:
        gate6["llm_model"] = val

    # 3. API Key — 写到 .env 而不是 config.yaml
    current = security.get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")
    val = _prompt_str("API key (leave empty to keep current)", default=current, password=True)
    if val and val != current:
        # 写入 .env 文件
        dotenv_path = CONFIG_PATH.parent.parent / ".env"
        existing = {}
        if dotenv_path.exists():
            for line in dotenv_path.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
        existing["OPENAI_API_KEY"] = val
        lines = "\n".join(f"{k}={v}" for k, v in existing.items())
        dotenv_path.write_text(lines + "\n")
        print(f"  API key saved to {dotenv_path} (OPENAI_API_KEY)")

    # 4. Gate0 mode per agent
    gate0 = gates.setdefault("gate0", {})
    per_agent = gate0.setdefault("per_agent", {})
    print()
    print("Gate0 — Permission mode per agent (observe=log only, enforce=block)")
    print("-" * 60)
    for agent in KNOWN_AGENTS:
        agent_cfg = per_agent.setdefault(agent, {})
        current = agent_cfg.get("mode", "enforce")
        val = _prompt_choice(f"  Mode for {agent}", ["observe", "enforce"], default=current)
        agent_cfg["mode"] = val

    # 5. Gate6 pass threshold per agent
    print()
    print("Gate6 — Pass threshold per agent (0.0-1.0)")
    print("-" * 60)
    current_global = gate6.get("pass_threshold", 0.58)
    per_agent_th = gate6.setdefault("per_agent", {})
    for agent in KNOWN_AGENTS:
        agent_cfg = per_agent_th.setdefault(agent, {})
        current = agent_cfg.get("pass_threshold", current_global)
        while True:
            val = _prompt_str(f"  Threshold for {agent}", default=str(current))
            if not val:
                break
            try:
                fval = float(val)
                if 0.0 <= fval <= 1.0:
                    agent_cfg["pass_threshold"] = fval
                    break
                print("  Must be between 0.0 and 1.0")
            except ValueError:
                print("  Must be a number")

    # 6. Storage backend
    print()
    storage = config.setdefault("storage", {})
    current = storage.get("backend", "memory")
    val = _prompt_choice("Storage backend", STORAGE_BACKENDS, default=current)
    storage["backend"] = val

    # Summary
    print()
    _display_config(config)
    if not _confirm("Write changes to config.yaml?"):
        print("Configuration cancelled. No changes written.")
        return config  # return unchanged
    return config


# ── Default config generator ───────────────────────────────────────


def _generate_default_config() -> dict:
    """Generate a clean default config with no internal or sensitive data."""
    return {
        "gates": {
            "mode": "memory",
            "gate0": {
                "agent_acl": {},
                "per_agent": {
                    "claude-code": {"mode": "observe"},
                    "qclaw": {"mode": "observe"},
                },
                "skip_arg_inspection": False,
            },
            "gate1": {
                "execution_time_tolerance": 1.2,
                "token_tolerance": 1.1,
                "consecutive_failures_before_escalation": 3,
                "circuit_breaker_cooldown_seconds": 60.0,
                "budgets": {
                    "default": {"token_budget": 10000, "time_budget_ms": 9000000},
                    "hermes": {"token_budget": 9972, "time_budget_ms": 8755070},
                    "claude-code": {"token_budget": 20000, "time_budget_ms": 600000},
                    "codex": {"token_budget": 15000, "time_budget_ms": 600000},
                    "opencode": {"token_budget": 15000, "time_budget_ms": 600000},
                },
            },
            "gate3": {
                "regress_pct": 0.95,
                "perf_degradation_limit": 0.05,
                "repeatability_threshold": 0.1,
                "repeatability_runs": 3,
                "unstable_retry_count": 5,
                "dynamic_baseline": True,
                "baseline_window": 20,
                "baseline_min_samples": 5,
                "auto_evolve_baseline": True,
                "per_agent": {
                    "hermes": {"regress_pct": 0.93, "perf_degradation_limit": 0.08},
                    "claude-code": {"regress_pct": 0.97, "perf_degradation_limit": 0.05},
                    "codex": {"regress_pct": 0.95, "perf_degradation_limit": 0.05},
                    "opencode": {"regress_pct": 0.95, "perf_degradation_limit": 0.06},
                },
            },
            "gate4": {
                "error_rate_increase": 0.01,
                "latency_increase": 0.1,
                "resource_increase": 0.15,
                "stable_period_seconds": 60,
            },
            "gate6": {
                "enabled": True,
                "evaluator": "checklist",
                "pass_threshold": 0.58,
                "timeout_seconds": 60.0,
                "fallback_on_timeout": "pass",
                "llm_model": "gpt-4o-mini",
                "llm_endpoint": "https://api.openai.com/v1",
                "per_agent": {
                    "hermes": {"pass_threshold": 0.58},
                    "claude-code": {"pass_threshold": 0.67},
                    "codex": {"pass_threshold": 0.58},
                    "opencode": {"pass_threshold": 0.58},
                },
            },
            "auto_fix": {
                "enabled": True,
                "max_retries": 3,
                "cooldown_minutes": 5,
                "stateful": True,
                "stage_min_duration_seconds": 0.1,
                "stage_error_threshold": 0.02,
                "stages": {
                    "1": {"traffic_pct": 1, "observe_cycles": 2, "label": "1%"},
                    "2": {"traffic_pct": 10, "observe_cycles": 4, "label": "10%"},
                    "3": {"traffic_pct": 50, "observe_cycles": 6, "label": "50%"},
                    "4": {"traffic_pct": 100, "observe_cycles": 0, "label": "100%"},
                },
            },
        },
        "tools": {
            "risk": {
                "benign": [
                    "read_file", "search_files", "session_search",
                    "skills_list", "skill_view", "memory", "vision_analyze",
                    "browser_navigate", "browser_snapshot", "browser_console",
                    "browser_vision", "browser_get_images", "browser_scroll",
                    "browser_back", "web_search", "process_poll", "process_log",
                    "process_list", "process", "todo", "execute_code",
                ],
                "elevated": [
                    "write_file", "patch", "skill_manage",
                    "browser_click", "browser_type", "browser_press",
                    "text_to_speech",
                ],
                "dangerous": [
                    "terminal", "shell_exec", "process_kill", "process_wait",
                    "process_submit", "process_write", "process_close",
                    "send_message", "delegate_task", "cronjob", "clarify",
                ],
            },
            "aliases": {
                "claude-code": {
                    "Read": "read_file", "Write": "write_file",
                    "Bash": "terminal", "Edit": "patch",
                    "Think": "todo", "Agent": "delegate_task",
                    "TaskCreate": "todo",
                },
                "codex": {
                    "read": "read_file", "write": "write_file",
                    "bash": "terminal",
                },
                "qclaw": {
                    "exec": "terminal", "read": "read_file",
                    "write": "write_file", "edit": "patch",
                },
            },
        },
        "storage": {
            "backend": "memory",
            "file_path": "./data/improvements.json",
        },
        "observability": {
            "otel": {"endpoint": "http://localhost:4317", "service_name": "agent-prod", "enabled": False},
            "metrics": {"provider": "demo", "prometheus_url": "http://localhost:9090", "timeout_seconds": 5.0},
        },
        "logging": {
            "format": "json",
            "level": "INFO",
            "output": "stdout",
        },
        "alerts": {"enabled": False},
        "sandbox": {
            "path_whitelist": ["/tmp/", "/var/tmp/"],
            "path_blacklist": ["/etc/passwd", "/etc/shadow", "/etc/sudoers"],
        },
    }


# ── Entry point ────────────────────────────────────────────────────


def cmd_configure(args: argparse.Namespace) -> None:  # noqa: F821
    """CLI entry point for 'agent-prod configure'."""
    gate7_mode = getattr(args, "gate7_mode", None)
    mode = getattr(args, "mode", None)
    agent = getattr(args, "agent", None)

    # ── Quick mode: agent-prod configure --gate7-mode enforce --agent my-agent ──
    if gate7_mode and agent:
        config = load_config() if CONFIG_PATH.exists() else _generate_default_config()
        gate7 = config.setdefault("gates", {}).setdefault("gate7", {})
        per_agent = gate7.setdefault("per_agent", {})
        if agent == "__global__":
            gate7["mode"] = gate7_mode
            print(f"Gate7 global mode set to '{gate7_mode}'.")
        else:
            per_agent[agent] = {"mode": gate7_mode}
            print(f"Gate7 mode for '{agent}' set to '{gate7_mode}'.")
        save_config(config)
        print("Restart the server to apply: agent-prod serve")
        return
    if gate7_mode and not agent:
        print("Error: --gate7-mode requires --agent <name> or use '__global__' for default")
        sys.exit(1)

    # ── Quick mode: agent-prod configure --mode observe --agent my-agent ──
    if mode and agent:
        config = load_config() if CONFIG_PATH.exists() else _generate_default_config()
        gate0 = config.setdefault("gates", {}).setdefault("gate0", {})
        per_agent = gate0.setdefault("per_agent", {})
        per_agent[agent] = {"mode": mode}
        save_config(config)
        print(f"Gate0 mode for '{agent}' set to '{mode}'.")
        print("Restart the server to apply: agent-prod serve")
        return
    if mode and not agent:
        print("Error: --mode requires --agent <name>")
        sys.exit(1)

    if args.reset:
        if not _confirm("Reset config to defaults? This will overwrite config.yaml.", default=False):
            print("Reset cancelled.")
            return
        save_config(_generate_default_config())
        print("Config reset to defaults.")
        return

    # Ensure config exists
    if not CONFIG_PATH.exists():
        if args.show:
            print(f"Config not found at {CONFIG_PATH}. Nothing to show.")
            sys.exit(1)
        print(f"Config not found at {CONFIG_PATH}. Creating default config first.")
        save_config(_generate_default_config())

    config = load_config()

    if args.show:
        _display_config(config)
        return

    # Interactive mode
    modified = _interactive_configure(config)
    save_config(modified)
    print("Configuration updated. Run 'agent-prod serve' to start the server.")
