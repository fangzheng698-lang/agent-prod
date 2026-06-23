"""Hermes CLI — the `agent-prod` command.

pip install agent-prod && agent-prod init   ← 3 minutes to running

Commands:
    agent-prod init       Interactive setup wizard
    agent-prod serve      Start the server
    agent-prod status     Health check
    agent-prod test       Run a quick end-to-end test
    agent-prod config     Show/set configuration
    agent-prod version    Show version
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import yaml

VERSION = "0.2.0"
CONFIG_DIR = Path.home() / ".agent-prod"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
ENV_FILE = CONFIG_DIR / ".env"
DEFAULT_DATA_DIR = CONFIG_DIR / "data"


# ═══════════════════════════════════════════
# Utils
# ═══════════════════════════════════════════

def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _banner():
    return """
  ╔══════════════════════════════════╗
  ║     agent-prod v{:<16}║
  ║  Enterprise AI Agent Framework  ║
  ╚══════════════════════════════════╝""".format(VERSION)


def _load_config():
    if CONFIG_FILE.exists():
        return yaml.safe_load(CONFIG_FILE.read_text()) or {}
    return {}


def _save_config(cfg: dict):
    _ensure_config_dir()
    CONFIG_FILE.write_text(yaml.dump(cfg, default_flow_style=False))


def _check_health(host="127.0.0.1", port=8000, timeout=5):
    import urllib.request
    import urllib.error
    try:
        url = f"http://{host}:{port}/health"
        resp = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(resp.read())
    except Exception:
        return None


# ═══════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════

@click.group()
@click.version_option(VERSION, prog_name="agent-prod")
def main():
    """Hermes CLI — Enterprise AI Agent Framework"""
    pass


@main.command()
def init():
    """Interactive setup wizard — get running in 3 minutes.

    Prompts for LLM credentials, creates config, starts the server.
    No external infrastructure required.
    """
    click.echo(_banner())
    click.echo()
    click.echo("  This wizard will set up agent-prod in ~3 minutes.")
    click.echo("  No Docker, no Postgres, no Prometheus required.")
    click.echo()

    # ── Prereqs ──
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    click.echo(f"  ✓ Python {pyver} detected")

    # Check if existing config
    if CONFIG_FILE.exists():
        if not click.confirm("  Existing config found. Overwrite?"):
            click.echo("  Aborted.")
            return

    # ── LLM Configuration ──
    click.echo()
    click.echo(click.style("  ── LLM Configuration ──", bold=True))
    api_key = click.prompt("  API Key", default="", hide_input=True, show_default=False)
    base_url = click.prompt("  Base URL", default="https://api.openai.com/v1")
    model = click.prompt("  Model", default="gpt-4o-mini")

    # ── Server Configuration ──
    click.echo()
    click.echo(click.style("  ── Server ──", bold=True))
    host = click.prompt("  Host", default="0.0.0.0")
    port = click.prompt("  Port", default=8000, type=int)

    # ── Quality Gates ──
    click.echo()
    click.echo(click.style("  ── Quality Gates ──", bold=True))
    click.echo("  Embedded mode: all gates run in-memory (no external services)")
    gates_mode = click.prompt(
        "  Mode", type=click.Choice(["embedded", "production"]), default="embedded"
    )

    # ── Build config ──
    config = {
        "server": {"host": host, "port": port},
        "llm": {
            "provider": "openai",
            "api_key": api_key if api_key else os.environ.get("OPENAI_API_KEY", ""),
            "base_url": base_url,
            "model": model,
        },
        "quality_gates": {
            "enabled": True,
            "mode": "memory" if gates_mode == "embedded" else "production",
            "config_path": "quality_gates/config.yaml",
        },
        "runtime": {
            "max_turns": 50,
            "max_tokens": 100_000,
            "budget_time_ms": 120_000,
        },
        "database": {
            "url": f"sqlite+aiosqlite:///{DEFAULT_DATA_DIR}/agent.db",
        },
        "observability": {
            "metrics": {"enabled": True},  # built-in /metrics
            "tracing": {"enabled": False},  # no Jaeger needed
        },
    }

    _ensure_config_dir()
    CONFIG_FILE.write_text(yaml.dump(config, default_flow_style=False))
    click.echo()
    click.echo(f"  ✓ Config saved to {CONFIG_FILE}")

    # ── Test connection ──
    click.echo()
    click.echo("  Testing LLM connection...")
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        urllib.request.urlopen(req, timeout=10)
        click.echo(click.style("  ✓ LLM connection OK", fg="green"))
    except Exception as e:
        click.echo(click.style(f"  ⚠ Could not verify LLM: {e}", fg="yellow"))
        click.echo("    You can update the key later: agent-prod config set llm.api_key YOUR_KEY")

    # ── Start server? ──
    click.echo()
    if click.confirm("  Start agent-prod server now?", default=True):
        click.echo()
        click.echo(click.style("  Starting agent-prod...", fg="green"))
        click.echo()

        env = os.environ.copy()
        env["AGENT_PROD_CONFIG"] = str(CONFIG_FILE)
        env["OPENAI_API_KEY"] = api_key
        env["OPENAI_BASE_URL"] = base_url
        env["OPENAI_MODEL"] = model

        # We wrap in a shell so the user sees output in foreground
        cmd = [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", host,
            "--port", str(port),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            import time
            time.sleep(3)

            health = _check_health(host, port)
            if health:
                click.echo(click.style(f"  ✓ Server running at http://{host}:{port}", fg="green"))
                click.echo(f"    Model: {health.get('model', '?')}")
                click.echo(f"    Quality gates: {health.get('quality_gates', '?')}")
            else:
                click.echo(click.style("  ⚠ Server started but health check failed", fg="yellow"))

            click.echo(f"    PID: {proc.pid}")
        except Exception as e:
            click.echo(click.style(f"  ✗ Failed to start: {e}", fg="red"))

    # ── Done ──
    click.echo()
    click.echo("  ╔══════════════════════════════════╗")
    click.echo("  ║  ✓ agent-prod is ready!          ║")
    click.echo("  ║                                  ║")
    click.echo("  ║  Quick test:                     ║")
    click.echo(f"  ║    agent-prod test               ║")
    click.echo("  ║                                  ║")
    click.echo("  ║  Commands:                       ║")
    click.echo("  ║    agent-prod serve  Start server║")
    click.echo(f"  ║    agent-prod status Health check║")
    click.echo("  ║    agent-prod config Edit config ║")
    click.echo("  ╚══════════════════════════════════╝")


@main.command()
@click.option("--host", default="0.0.0.0", help="Bind address")
@click.option("--port", default=8000, help="Port")
@click.option("--background/--foreground", default=False, help="Detach from terminal")
def serve(host, port, background):
    """Start the agent-prod server."""
    click.echo(_banner())

    cfg = _load_config()
    env = os.environ.copy()

    if cfg:
        env["AGENT_PROD_CONFIG"] = str(CONFIG_FILE)
        llm = cfg.get("llm", {})
        if llm.get("api_key"):
            env["OPENAI_API_KEY"] = llm["api_key"]
        if llm.get("base_url"):
            env["OPENAI_BASE_URL"] = llm["base_url"]
        if llm.get("model"):
            env["OPENAI_MODEL"] = llm["model"]

    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", host, "--port", str(port),
    ]

    project_root = str(Path(__file__).resolve().parent.parent)

    if background:
        click.echo(f"  Starting in background on {host}:{port}...")
        proc = subprocess.Popen(cmd, env=env, cwd=project_root,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time; time.sleep(2)
        health = _check_health(host, port)
        if health:
            click.echo(click.style(f"  ✓ Running (PID: {proc.pid})", fg="green"))
        else:
            click.echo(click.style(f"  ⚠ Started but health check failed (PID: {proc.pid})", fg="yellow"))
    else:
        click.echo(f"  Starting on {host}:{port}...")
        subprocess.run(cmd, env=env, cwd=project_root)


@main.command()
@click.option("--host", default="127.0.0.1", help="Server host")
@click.option("--port", default=8000, help="Server port")
def status(host, port):
    """Check server health."""
    health = _check_health(host, port)
    if health:
        click.echo(click.style(f"✓ agent-prod is running on {host}:{port}", fg="green"))
        click.echo(f"  Status:   {health.get('status', '?')}")
        click.echo(f"  Model:    {health.get('model', '?')}")
        click.echo(f"  Sessions: {health.get('sessions_active', '?')}")
        click.echo(f"  Gates:    {'enabled' if health.get('quality_gates') else 'disabled'}")
        click.echo(f"  Metrics:  http://{host}:{port}/metrics")
    else:
        click.echo(click.style(f"✗ No response from {host}:{port}", fg="red"))
        click.echo(f"  Is the server running? Try: agent-prod serve")
        sys.exit(1)


@main.command()
@click.option("--host", default="127.0.0.1", help="Server host")
@click.option("--port", default=8000, help="Server port")
@click.option("--prompt", default="What is 2+2?", help="Test prompt")
def test(host, port, prompt):
    """Run a quick end-to-end test query."""
    import urllib.request

    url = f"http://{host}:{port}/v1/chat/completions"
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "session_id": "agent-prod_cli_test",
        "max_tokens": 100,
    }).encode()

    click.echo(f"  Sending to {url}...")
    click.echo(f'  Prompt: "{prompt}"')
    click.echo()

    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())

        choices = data.get("choices", [{}])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        qg = data.get("quality_gate", {})

        click.echo(click.style("  ✓ Got response", fg="green"))
        click.echo(f"  Content: {content[:200]}")
        click.echo(f"  Quality gate: {qg.get('status', '?')} ({'PASS' if qg.get('passed') else 'FAIL'})")
        if qg.get("gates"):
            for g in qg["gates"]:
                status = "✓" if g["passed"] else "✗"
                click.echo(f"    {status} {g['gate']} ({g.get('duration_ms', '?')}ms)")
    except Exception as e:
        click.echo(click.style(f"  ✗ Failed: {e}", fg="red"))
        sys.exit(1)


@main.group()
def config():
    """View or edit configuration."""
    pass


@config.command("show")
def config_show():
    """Display current configuration."""
    if not CONFIG_FILE.exists():
        click.echo("No config found. Run: agent-prod init")
        return

    cfg = _load_config()
    click.echo(yaml.dump(cfg, default_flow_style=False))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a config value. E.g.: agent-prod config set llm.model gpt-4"""
    cfg = _load_config()
    keys = key.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value
    _save_config(cfg)
    click.echo(f"  ✓ {key} = {value}")


@config.command("path")
def config_path():
    """Show config file location."""
    click.echo(str(CONFIG_FILE))


@config.command("reset")
def config_reset():
    """Reset configuration to defaults."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    click.echo("  ✓ Config reset. Run: agent-prod init")


@main.command()
def version():
    """Show version and environment info."""
    click.echo(f"agent-prod {VERSION}")
    click.echo(f"Python {sys.version}")
    cfg = _load_config()
    if cfg:
        llm = cfg.get("llm", {})
        click.echo(f"Config: {CONFIG_FILE}")
        click.echo(f"Model: {llm.get('model', '?')}")
        click.echo(f"Gates: {cfg.get('quality_gates', {}).get('mode', '?')}")


# ═══════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════

if __name__ == "__main__":
    main()
