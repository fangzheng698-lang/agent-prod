#!/usr/bin/env python3
"""Backfill all QClaw sessions through gate pipeline and summarize results."""
import json, time, sys, os
from pathlib import Path
from urllib import request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from agent_prod.integration.qclaw_parser import parse_qclaw_session, list_qclaw_sessions

URL = "http://localhost:9002/v1/agent/evaluate"
session_paths = list_qclaw_sessions()
total = len(session_paths)
print(f"Backfilling {total} qclaw sessions...", flush=True)

stats = {"PRODUCTION": 0, "CANDIDATE": 0, "REJECTED": 0, "error": 0, "none": 0}
gate_stats = {}
fail_reasons = []

for i, fpath in enumerate(session_paths):
    sid = fpath.stem
    try:
        payload = parse_qclaw_session(str(fpath))
        if not payload:
            stats["none"] += 1
            if i % 20 == 0: print(f"  [{i+1}] {sid[:24]} NONE", flush=True)
            continue
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(URL, data=data, headers={"Content-Type":"application/json"}, method="POST")
        with request.urlopen(req, timeout=120) as resp:
            r = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        stats["error"] += 1
        if i % 20 == 0: print(f"  [{i+1}] {sid[:24]} ERR {type(e).__name__}: {str(e)[:120]}", flush=True)
        continue

    status = (r.get("status") or "?").upper()
    stats[status] = stats.get(status, 0) + 1
    for g in r.get("gates", []):
        gn = (g.get("gate") or g.get("gate_name") or "?").upper()
        gate_stats.setdefault(gn, {"pass":0, "fail":0})
        gate_stats[gn]["pass" if g.get("passed") else "fail"] += 1

    fr = r.get("failed_at") or r.get("failed_gate")
    if fr:
        fail_reasons.append(fr)

    if i % 25 == 0 or i == total - 1:
        passed_gates = sum(1 for g in r.get("gates",[]) if g.get("passed"))
        print(f"  [{i+1}/{total}] {sid[:24]} status={status} gates={passed_gates}/{len(r.get('gates',[]))}", flush=True)
        if not r.get("passed"):
            for g in r.get("gates",[]):
                if not g.get("passed"):
                    print(f"         fail: {(g.get('gate') or g.get('gate_name'))}: {(g.get('reason') or '?')[:120]}", flush=True)

print(f"\n=== Distribution ===", flush=True)
for k, v in sorted(stats.items()):
    print(f"  {k}: {v}", flush=True)
print(f"\n=== Per-gate pass/fail ===", flush=True)
for gn, st in sorted(gate_stats.items()):
    rate = st["pass"] / max(st["pass"]+st["fail"],1) * 100
    print(f"  {gn}: {st['pass']}P/{st['fail']}F ({rate:.0f}%)", flush=True)
