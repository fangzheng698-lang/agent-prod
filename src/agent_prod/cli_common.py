"""Shared constants and helpers for agent-prod CLI modules.

Avoids circular imports between cli.py and cli_*.py modules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Project root — walk up from this file (which lives in src/agent_prod/)
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "gates" / "config.yaml"


def live_server() -> str:
    import os
    return os.environ.get("AGENT_PROD_URL", "http://localhost:8000")


def load_config() -> dict:
    import yaml
    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict) -> None:
    import yaml
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"Saved {CONFIG_PATH}")
