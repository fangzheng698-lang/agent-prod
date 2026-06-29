"""
Per-agent threshold resolution.

Gate3 and Gate4 thresholds are defined globally in config.yaml with optional
per-agent overrides. This module merges the two: when an improvement carries
an agent type in its metadata, per-agent overrides take precedence over global defaults.

Usage:
    from agent_prod.gates.thresholds import resolve_agent_thresholds

    resolved = resolve_agent_thresholds("gate3", "hermes", config_dict)
    # → {"regress_pct": 0.93, "perf_degradation_limit": 0.08, ...}
"""

from __future__ import annotations

from typing import Any

# Known per-agent threshold keys for each gate — only these keys
# are resolved per-agent; everything else stays global.
_GATE3_PER_AGENT_KEYS = {
    "regress_pct",
    "perf_degradation_limit",
    "repeatability_threshold",
    "repeatability_runs",
    "unstable_retry_count",
}

_GATE4_PER_AGENT_KEYS = {
    "error_rate_increase",
    "latency_increase",
    "resource_increase",
    "stable_period_seconds",
}

_PER_AGENT_KEYS_MAP: dict[str, set[str]] = {
    "gate3": _GATE3_PER_AGENT_KEYS,
    "gate4": _GATE4_PER_AGENT_KEYS,
}


def resolve_agent_thresholds(
    gate_name: str,
    agent_type: str,
    config: dict | None,
) -> dict[str, Any]:
    """
    Resolve thresholds for a specific (gate, agent_type) pair.

    Resolution order:
      1. Read global gate config from config["gates"][gate_name]
      2. If config["gates"][gate_name]["per_agent"][agent_type] exists,
         overlay those values on the global defaults.
      3. Return the merged dict (global defaults with agent overrides).

    If config is None or the gate section is missing, returns empty dict.
    The per_agent section itself is stripped from the result — it's consumed
    during resolution and not a threshold value.

    Args:
        gate_name: "gate3" or "gate4"
        agent_type: "hermes", "claude-code", "codex", "opencode", etc.
        config: Full YAML config dict (the raw dict from load_config)

    Returns:
        Merged threshold dict ready to pass to Gate3Config / Gate4Config.
    """
    if not config:
        return {}

    gate_cfg = config.get("gates", {}).get(gate_name, {})
    if not gate_cfg:
        return {}

    # Start with global defaults (strip per_agent section)
    resolved: dict[str, Any] = {
        k: v for k, v in gate_cfg.items()
        if k != "per_agent"
    }

    # Overlay agent-specific overrides for the relevant keys
    per_agent = gate_cfg.get("per_agent", {})
    agent_overrides = per_agent.get(agent_type, {})

    per_agent_keys = _PER_AGENT_KEYS_MAP.get(gate_name, set())
    for key in per_agent_keys:
        if key in agent_overrides:
            resolved[key] = agent_overrides[key]

    return resolved


def list_agents_with_overrides(
    gate_name: str,
    config: dict | None,
) -> list[str]:
    """Return agent types that have custom overrides for this gate."""
    if not config:
        return []
    gate_cfg = config.get("gates", {}).get(gate_name, {})
    per_agent = gate_cfg.get("per_agent", {})
    return sorted(per_agent.keys())
