# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""agent-prod feedback — query flywheel improvement suggestions.

Usage:
    agent-prod feedback                    list all improvements
    agent-prod feedback --id <id>          show improvement detail
    agent-prod feedback --apply <id>       apply an improvement to config

Exports:
    cmd_feedback(args: argparse.Namespace) -> None
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_prod.cli_common import CONFIG_PATH, load_config, save_config


# ── Local repository readers ───────────────────────────────────────


def _load_improvements() -> dict:
    """Load improvements from local file repository."""
    config = load_config()
    storage = config.get("storage", {})
    file_path = storage.get("file_path", "./data/improvements.json")
    fpath = Path(file_path)
    if not fpath.exists():
        return {}
    try:
        return json.loads(fpath.read_text())
    except Exception:
        return {}


# ── Display ────────────────────────────────────────────────────────


def _display_feedback_list(improvements: dict) -> None:
    """Display improvement list as a table."""
    if not improvements:
        print("No improvements found.")
        return

    print()
    print("agent-prod Flywheel Improvements")
    print("=" * 60)
    print(f"  {'ID':<24} {'Agent':<16} {'Status':<12} {'Score':<8} {'Name'}")
    print(f"  {'-'*22:<24} {'-'*14:<16} {'-'*10:<12} {'-'*6:<8} {'-'*30}")

    for imp_id in sorted(improvements.keys()):
        imp = improvements[imp_id]
        agent = imp.get("metadata", {}).get("agent", "?")[:14] if imp.get("metadata") else "?"
        status = imp.get("status", "?")[:10]
        score = imp.get("gate_results", [{}])[-1].get("score", "")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "-"
        name = imp.get("name", "")[:28]
        print(f"  {imp_id:<24} {agent:<16} {status:<12} {score_str:<8} {name}")

    print()


def _display_feedback_detail(imp_id: str, imp: dict) -> None:
    """Display detailed view of a single improvement."""
    print()
    print("agent-prod Improvement Detail")
    print("=" * 60)
    print(f"  ID:       {imp_id}")
    print(f"  Name:     {imp.get('name', '?')}")
    print(f"  Status:   {imp.get('status', '?')}")

    # Metadata
    meta = imp.get("metadata", {}) or {}
    if meta:
        print()
        print("  Metadata:")
        for key in ("agent", "session_id", "source", "subagent_task", "parent_session"):
            if key in meta:
                print(f"    {key}: {meta[key]}")

    # Output / final response
    output = imp.get("output", {}) or {}
    fr = output.get("final_response", "")
    if fr:
        print()
        print(f"  Final Response: {fr[:300]}")
        if len(fr) > 300:
            print("  ... (truncated)")

    # Gate results
    gate_results = imp.get("gate_results", [])
    if gate_results:
        print()
        print(f"  Gate Results ({len(gate_results)} gates):")
        for g in gate_results:
            g_name = g.get("gate_name", "?")
            g_passed = g.get("passed", False)
            g_score = g.get("score", "")
            g_detail = g.get("details", g.get("reason", ""))
            icon = "PASS" if g_passed else "FAIL"
            score_str = f" ({g_score})" if isinstance(g_score, (int, float)) else ""
            print(f"    {g_name:<12} {icon:<6}{score_str}  {str(g_detail)[:80]}")

    print()


# ── Apply improvement ──────────────────────────────────────────────


def _apply_improvement(imp_id: str, imp: dict) -> bool:
    """Apply an improvement's suggestions to config.yaml.

    This reads the improvement's suggested_config or gate_results
    and attempts to merge thresholds into the local config.
    """
    config = load_config()

    suggested = imp.get("suggested_config", {})
    if not suggested:
        # Try to extract from gate results
        gate_results = imp.get("gate_results", [])
        for g in gate_results:
            if g.get("suggested_config"):
                suggested = g["suggested_config"]
                break

    if not suggested:
        # Apply baseline output as candidate
        baseline_output = imp.get("baseline_output")
        if baseline_output:
            suggested = {"baseline": baseline_output}

    if not suggested:
        print(f"Improvement '{imp_id}' has no applicable suggestions.")
        return False

    # Merge suggestions into config
    gates = config.setdefault("gates", {})
    for gate_name, gate_config in suggested.items():
        if gate_name.startswith("gate"):
            if isinstance(gate_config, dict):
                for key, val in gate_config.items():
                    gates.setdefault(gate_name, {})[key] = val

    save_config(config)
    print(f"Applied improvement '{imp_id}' to config.yaml.")
    return True


# ── Entry point ────────────────────────────────────────────────────


def cmd_feedback(args: argparse.Namespace) -> None:  # noqa: F821
    """CLI entry point for 'agent-prod feedback'."""
    improvements = _load_improvements()

    if not improvements:
        print("No improvements found in local repository.")
        print("Start the server and submit evaluations first: agent-prod serve")
        sys.exit(1)

    # --apply
    if args.apply:
        imp = improvements.get(args.apply)
        if not imp:
            print(f"Improvement '{args.apply}' not found.")
            sys.exit(1)
        _apply_improvement(args.apply, imp)
        return

    # --id
    if args.id:
        imp = improvements.get(args.id)
        if not imp:
            print(f"Improvement '{args.id}' not found.")
            sys.exit(1)
        _display_feedback_detail(args.id, imp)
        return

    # Default: list all
    _display_feedback_list(improvements)