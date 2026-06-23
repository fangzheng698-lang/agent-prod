"""配置系统。pydantic-settings 自动从 .env 和环境变量加载。

Phase 10: Embedded-first defaults — no external infrastructure required.
Set quality_gates_mode=production to enable Postgres/Prometheus/Jaeger/Unleash.
"""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM ──
    llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # ── Server ──
    host: str = "0.0.0.0"
    port: int = 8000

    # ── Storage ──
    database_url: str = "sqlite+aiosqlite:///./data/agent.db"

    # ── Agent ──
    max_turns: int = 50
    max_tokens: int = 100_000

    # ── Quality Gates ──
    # Embedded mode (default): all gates run in-memory, zero external deps.
    # Production mode: Postgres + Prometheus + Jaeger + Unleash via config.yaml.
    quality_gates_enabled: bool = True
    quality_gates_config: str = "quality_gates/config.yaml"
    quality_gates_mode: str = "memory"  # "memory" | "production"

    # ── Observability ──
    metrics_enabled: bool = True


# ── Override from HERMES_CONFIG YAML (hermes CLI) ──
def _load_hermes_config() -> dict:
    """Load settings from agent-prod CLI config file if present."""
    config_path = os.environ.get("AGENT_PROD_CONFIG", "")
    if not config_path or not Path(config_path).exists():
        return {}
    import yaml
    try:
        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    except Exception:
        return {}

    # Map hermes config structure to Settings fields
    overrides = {}
    llm = cfg.get("llm", {})
    if llm.get("api_key"):
        overrides["openai_api_key"] = llm["api_key"]
    if llm.get("base_url"):
        overrides["openai_base_url"] = llm["base_url"]
    if llm.get("model"):
        overrides["openai_model"] = llm["model"]

    server = cfg.get("server", {})
    if server.get("host"):
        overrides["host"] = server["host"]
    if server.get("port"):
        overrides["port"] = server["port"]

    db = cfg.get("database", {})
    if db.get("url"):
        overrides["database_url"] = db["url"]

    runtime = cfg.get("runtime", {})
    if runtime.get("max_turns"):
        overrides["max_turns"] = runtime["max_turns"]
    if runtime.get("max_tokens"):
        overrides["max_tokens"] = runtime["max_tokens"]

    gates = cfg.get("quality_gates", {})
    if "enabled" in gates:
        overrides["quality_gates_enabled"] = gates["enabled"]
    if gates.get("mode"):
        overrides["quality_gates_mode"] = gates["mode"]
    if gates.get("config_path"):
        overrides["quality_gates_config"] = gates["config_path"]

    return overrides


# Build settings with cascade: defaults < .env file < HERMES_CONFIG YAML < env vars
_settings_overrides = _load_hermes_config()
settings = Settings(**_settings_overrides)
