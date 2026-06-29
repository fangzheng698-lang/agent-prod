#!/usr/bin/env python3
"""一次性回放历史 82 个 session 到 agent-prod。

从 Hermes state.db 读取所有已结束 session，
逐个构建 AgentTrace payload 并 POST 到 agent-prod :8765。
"""
import json
import os
import sqlite3
import sys
import time
from urllib import request

# 强制指到正确的 agent-prod 端口
os.environ["AGENT_PROD_URL"] = "http://localhost:8765"

# 切到 Hermes 环境
HERMES_HOME = os.path.expanduser("~/.hermes")
sys.path.insert(0, os.path.join(HERMES_HOME, "hermes-agent"))

from agent_prod.integration.hermes_evaluator import (
    _build_trace_payload,
    _post_evaluate,
)
from hermes_state import SessionDB


def main():
    db_path = os.path.join(HERMES_HOME, "state.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NOT NULL ORDER BY ended_at ASC"
    ).fetchall()

    conn.close()

    total = len(rows)
    print(f"共 {total} 个已结束 session，开始回放...")

    session_db = SessionDB()
    ok = 0
    fail = 0
    results = []

    for i, row in enumerate(rows):
        sid = row["id"]
        session = dict(row)

        # 获取消息
        messages = session_db.get_messages(sid)
        if not messages:
            fail += 1
            continue

        # 构建 trace payload
        try:
            payload = _build_trace_payload(session, messages)
        except Exception as e:
            print(f"  [{i+1}/{total}] {sid[:20]} 构建失败: {e}")
            fail += 1
            continue

        # POST 到 agent-prod
        result = _post_evaluate(payload)

        if result:
            status = result.get("status", "?")
            gates = len(result.get("gates", []))
            results.append((sid, status, gates))
            ok += 1
            print(f"  [{i+1}/{total}] {sid[:20]} → {status} ({gates} gates)")
        else:
            fail += 1
            print(f"  [{i+1}/{total}] {sid[:20]} → POST 失败")

    # ── 汇总 ──
    print(f"\n===== 回放完成 =====")
    print(f"成功: {ok}/{total}  失败: {fail}/{total}")

    prod = sum(1 for _, s, _ in results if s == "production")
    rej = sum(1 for _, s, _ in results if s == "rejected")
    print(f"PRODUCTION: {prod}  REJECTED: {rej}")

    # 按 gate 统计拒绝
    gate_rejections = {}
    for sid, s, g in results:
        pass
    print()

    # 检查 improvement 数量
    import json as _json
    imp_path = "/var/lib/quality_gates/improvements.json"
    try:
        with open(imp_path) as f:
            data = _json.load(f)
        print(f"improvements.json 记录数: {len(data)}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
