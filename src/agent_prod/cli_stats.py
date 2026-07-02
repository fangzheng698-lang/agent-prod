# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""agent-prod stats — query quality gate evaluation statistics.

Usage:
    agent-prod stats                        show summary
    agent-prod stats --agent qclaw          filter by agent
    agent-prod stats --rejected             show only rejected
    agent-prod stats --detail <id>          show evaluation detail
    agent-prod stats --plan-report          show plan vs execution consistency report

Exports:
    cmd_stats(args: argparse.Namespace) -> None
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from agent_prod.cli_common import load_config, live_server


# ── Live server queries ────────────────────────────────────────────


def _fetch_stats(agent: str = "", limit: int = 100) -> dict | None:
    """Query /v1/agent/stats from running server."""
    base = live_server()
    url = f"{base}/v1/agent/stats?limit={limit}"
    if agent:
        url += f"&agent={agent}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _fetch_improvement_detail(imp_id: str) -> dict | None:
    """Fetch a single improvement by ID from the server."""
    base = live_server()
    url = f"{base}/v1/agent/stats?limit=500"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for r in data.get("recent", []):
            if r.get("id") == imp_id:
                return r
        return None
    except Exception:
        return None


# ── Display ────────────────────────────────────────────────────────


def _display_stats_summary(stats: dict) -> None:
    """Display a summary table of evaluation stats."""
    total = stats.get("total", 0)
    by_status = stats.get("by_status", {})
    recent = stats.get("recent", [])

    print()
    print("agent-prod Evaluation Statistics")
    print("=" * 60)
    print(f"  Total submissions: {total}")
    if by_status:
        for status in ("production", "rejected", "candidate", "pending", "failed"):
            count = by_status.get(status, 0)
            if count > 0:
                print(f"    {status:<12} {count}")
    print()

    # Per-agent breakdown from recent records
    if recent:
        agent_stats: dict[str, dict[str, int]] = {}
        for r in recent:
            agent = r.get("agent", "unknown")
            status = r.get("status", "unknown")
            if agent not in agent_stats:
                agent_stats[agent] = {"total": 0, "production": 0, "rejected": 0, "other": 0}
            agent_stats[agent]["total"] += 1
            if status == "production":
                agent_stats[agent]["production"] += 1
            elif status == "rejected":
                agent_stats[agent]["rejected"] += 1
            else:
                agent_stats[agent]["other"] += 1

        print(f"  {'Agent':<20} {'Total':<6} {'Passed':<8} {'Rejected':<10} {'Pass Rate':<10}")
        print(f"  {'-'*18:<20} {'-'*4:<6} {'-'*6:<8} {'-'*8:<10} {'-'*8:<10}")
        for agent in sorted(agent_stats.keys()):
            s = agent_stats[agent]
            rate = f"{s['production']/s['total']*100:.1f}%" if s['total'] > 0 else "-"
            print(f"  {agent:<20} {s['total']:<6} {s['production']:<8} {s['rejected']:<10} {rate:<10}")
        print()

    # Recent evaluations
    print(f"  Recent evaluations ({len(recent)} shown):")
    print(f"  {'ID':<24} {'Agent':<16} {'Status':<12} {'Gates':<8} {'Time':<20}")
    print(f"  {'-'*22:<24} {'-'*14:<16} {'-'*10:<12} {'-'*6:<8} {'-'*18:<20}")
    for r in recent[:20]:  # show max 20 in recent list
        imp_id = r.get("id", "?")[:20]
        agent = r.get("agent", "?")[:14]
        status = r.get("status", "?")[:10]
        gates = f"{r.get('gates_passed', 0)}/{r.get('gates_total', 0)}"
        created = r.get("created_at", "")[:16] if r.get("created_at") else ""
        print(f"  {imp_id:<24} {agent:<16} {status:<12} {gates:<8} {created:<20}")
    print()


def _display_evaluation_detail(imp: dict) -> None:
    """Display detailed view of a single evaluation."""
    print()
    print("agent-prod Evaluation Detail")
    print("=" * 60)
    print(f"  ID:       {imp.get('id', '?')}")
    print(f"  Agent:    {imp.get('agent', '?')}")
    print(f"  Status:   {imp.get('status', '?')}")
    print(f"  Gates:    {imp.get('gates_passed', 0)}/{imp.get('gates_total', 0)} passed")
    print(f"  Created:  {imp.get('created_at', '?')}")
    print()

    # Show gate results if available
    gate_results = imp.get("gate_results", [])
    if gate_results:
        print(f"  Gate Results:")
        for gr in gate_results:
            gate_name = gr.get("gate_name", "?")
            passed = gr.get("passed", False)
            reason = gr.get("reason", "")[:100]
            icon = "✅" if passed else "❌"
            print(f"    {icon} {gate_name:<30s} {reason}")
        print()

    # Show plan consistency details (Gate7)
    for gr in gate_results:
        if "gate7" in str(gr.get("gate_name", "")):
            details = gr.get("details", {})
            if details.get("deviations"):
                print(f"  Plan Consistency Deviations:")
                for dev in details["deviations"]:
                    sev = dev.get("severity", "info")
                    sev_icon = "🔴" if sev == "critical" else ("🟡" if sev == "warning" else "⚪")
                    print(f"    {sev_icon} {dev.get('type', '?')}: {dev.get('detail', '')[:150]}")
            print()


def _display_plan_report(stats: dict) -> None:
    """Display plan vs execution consistency report."""
    recent = stats.get("recent", [])
    print()
    print("=" * 65)
    print("  Plan vs Execution Consistency Report")
    print("  Agent 计划 vs 实际执行一致性报告")
    print("=" * 65)
    print()

    # Filter records that have expected_plan in candidate_output or metadata
    plan_records = []
    for r in recent:
        candidate = r.get("candidate_output", r.get("current_metrics", {}))
        if not isinstance(candidate, dict):
            candidate = {}
        metadata = r.get("metadata", {})
        expected_plan = candidate.get("expected_plan", "") or metadata.get("expected_plan", "")
        if expected_plan:
            plan_records.append(r)

    if not plan_records:
        print("  No evaluation records with expected_plan found.")
        print("  Submit traces with candidate_output.expected_plan or")
        print("  metadata.expected_plan to enable plan consistency tracking.")
        print()
        return

    # Group by agent
    from collections import defaultdict
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in plan_records:
        by_agent[r.get("agent", "unknown")].append(r)

    total_ok = total_off = total_partial = 0

    for agent in sorted(by_agent.keys()):
        records = by_agent[agent]
        print(f"  Agent: {agent} ({len(records)} records)")
        print(f"  {'─'*60}")

        for r in records:
            candidate = r.get("candidate_output", r.get("current_metrics", {}))
            if not isinstance(candidate, dict):
                candidate = {}
            metadata = r.get("metadata", {})
            expected_plan = candidate.get("expected_plan", "") or metadata.get("expected_plan", "")
            final_resp = candidate.get("final_response", "")
            status = r.get("status", "?")
            gate_results = r.get("gate_results", [])

            # Check Gate7 for deviations
            gate7_result = None
            for gr in gate_results:
                if "gate7" in str(gr.get("gate_name", "")):
                    gate7_result = gr
                    break

            # Determine deviation status
            if gate7_result:
                details = gate7_result.get("details", {})
                deviations = details.get("deviations", [])
                critical_devs = [d for d in deviations if d.get("severity") == "critical"]
                warning_devs = [d for d in deviations if d.get("severity") == "warning"]

                if critical_devs:
                    icon = "❌"
                    label = "Off-plan"
                    total_off += 1
                elif warning_devs:
                    icon = "⚠️"
                    label = "Partial"
                    total_partial += 1
                else:
                    icon = "✅"
                    label = "On-plan"
                    total_ok += 1
            else:
                # No Gate7 — check if Gate6 follows_plan was caught
                g6_follows = None
                for gr in gate_results:
                    if "gate6" in str(gr.get("gate_name", "")):
                        checks = gr.get("details", {}).get("checks", {})
                        g6_follows = checks.get("follows_plan") if isinstance(checks, dict) else None
                        break

                if status == "production":
                    icon = "✅"
                    label = "On-plan" if g6_follows is not False else "Unknown"
                    total_ok += 1
                elif status == "rejected":
                    icon = "❌"
                    label = "Rejected"
                    total_off += 1
                else:
                    icon = "➖"
                    label = status
                    total_partial += 1

            # Show deviation details
            plan_snippet = expected_plan[:60] + "..." if len(expected_plan) > 60 else expected_plan
            resp_snippet = final_resp[:60].replace("\n", " ") + "..." if len(final_resp) > 60 else final_resp.replace("\n", " ")
            print(f"    {icon} [{label:<8}] Plan: {plan_snippet}")
            print(f"         Response: {resp_snippet}")

            if gate7_result:
                details = gate7_result.get("details", {})
                deviations = details.get("deviations", [])
                for dev in deviations[:3]:  # show top 3
                    sev = dev.get("severity", "info")
                    sev_char = "!" if sev == "critical" else "?"
                    print(f"           {sev_char} {dev.get('detail', '')[:120]}")
            print()

    # Summary
    print(f"  {'═'*60}")
    print(f"  Summary:")
    print(f"    ✅ On-plan:  {total_ok}")
    print(f"    ⚠️ Partial:  {total_partial}")
    print(f"    ❌ Off-plan: {total_off}")
    print(f"    Total with expected_plan: {len(plan_records)}")
    print(f"  {'═'*60}")
    print(f"  Gate7 (Execution Consistency) is now active: detects plan")
    print(f"  deviations via keyword analysis. Gate6 follows_plan dimension")
    print(f"  provides LLM-based plan adherence assessment.")
    print()


# ── Entry point ────────────────────────────────────────────────────


def cmd_stats(args: argparse.Namespace) -> None:  # noqa: F821
    """CLI entry point for 'agent-prod stats'."""
    # --detail overrides all other modes
    if args.detail:
        imp = _fetch_improvement_detail(args.detail)
        if imp:
            _display_evaluation_detail(imp)
            return
        config = load_config()
        storage = config.get("storage", {})
        file_path = storage.get("file_path", "./data/improvements.json")
        fpath = Path(file_path)
        if fpath.exists():
            try:
                data = json.loads(fpath.read_text())
                if args.detail in data:
                    _display_evaluation_detail(data[args.detail])
                    return
            except Exception:
                pass
        print(f"Evaluation '{args.detail}' not found.")
        sys.exit(1)

    # Summary mode — support comma-separated agents
    agent_filter = args.agent or ""
    agents = [a.strip() for a in agent_filter.split(",") if a.strip()] if agent_filter else []
    stats = _fetch_stats(agent="", limit=200)

    if not stats:
        print("Server not available. Cannot query stats.")
        print("Start the server with: agent-prod serve")
        sys.exit(1)

    # --plan-report overrides other display modes
    if args.plan_report:
        recent = stats.get("recent", [])
        if agents:
            recent = [r for r in recent if r.get("agent", "") in agents]
            stats["recent"] = recent
        _display_plan_report(stats)
        return

    if stats:
        recent = stats.get("recent", [])
        # Filter by multiple agents if specified
        if agents:
            recent = [r for r in recent if r.get("agent", "") in agents]
            # Rebuild by_status and total from filtered data
            by_status: dict[str, int] = {}
            for r in recent:
                s = r.get("status", "unknown")
                by_status[s] = by_status.get(s, 0) + 1
            stats["recent"] = recent
            stats["total"] = len(recent)
            stats["by_status"] = by_status
        elif args.rejected:
            stats["recent"] = [r for r in recent if r.get("status") == "rejected"]
            stats["total"] = len(stats["recent"])
        _display_stats_summary(stats)
    else:
        print("Server not available. Cannot query stats.")
        print("Start the server with: agent-prod serve")
        sys.exit(1)