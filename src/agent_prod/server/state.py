# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""SQLite 持久化状态管理。使用标准 sqlite3。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path


class StateStore:
    """
    会话持久化存储。

    线程安全（使用 SQLite WAL 模式）。
    同步 API（SQLite 写入纳秒级，不需要异步）。
    """

    def __init__(self, db_url: str):
        # sqlite+aiosqlite:///./data/agent.db → ./data/agent.db
        path = db_url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            with self._connections_lock:
                self._connections.add(self._local.conn)
        return self._local.conn

    def close(self) -> None:
        """Close all SQLite connections opened by this store."""
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            with suppress(sqlite3.Error):
                conn.close()
        if hasattr(self._local, "conn"):
            self._local.conn = None

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                status TEXT DEFAULT 'active',
                messages TEXT DEFAULT '[]',
                meta_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proxy_sessions (
                id TEXT PRIMARY KEY,
                meta_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        conn.commit()

    # ── 会话生命周期 ──

    def create_session(self, session_id: str, meta: dict | None = None):
        now = datetime.now(UTC).isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO sessions (id, status, messages, meta_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "active", "[]", json.dumps(meta or {}, ensure_ascii=False), now, now),
        )
        conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def update_status(self, session_id: str, status: str, error: str | None = None):
        conn = self._get_conn()
        now = datetime.now(UTC).isoformat()
        if error:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ?, error = ? WHERE id = ?",
                (status, now, error, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, session_id),
            )
        conn.commit()

    # ── 消息操作 ──

    def get_messages(self, session_id: str) -> list[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT messages FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return []
        return json.loads(row["messages"])

    def save_checkpoint(self, session_id: str, messages: list[dict]):
        conn = self._get_conn()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE sessions SET messages = ?, updated_at = ? WHERE id = ?",
            (json.dumps(messages, ensure_ascii=False), now, session_id),
        )
        conn.commit()

    def list_sessions(self, limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, status, messages, created_at, updated_at, error FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "status": r["status"],
                "n_messages": len(json.loads(r["messages"])),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "error": r["error"],
            }
            for r in rows
        ]

    # ── Proxy Sessions ──

    def _upsert_proxy_session(self, session_id: str, meta: dict) -> None:
        """Upsert proxy session metadata for dashboard queries."""
        now = datetime.now(UTC).isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO proxy_sessions (id, meta_json, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   meta_json = excluded.meta_json,
                   updated_at = excluded.updated_at""",
            (session_id, json.dumps(meta, ensure_ascii=False), now, now),
        )
        conn.commit()

    def list_proxy_sessions(self, limit: int = 50) -> list[dict]:
        """List proxy sessions for dashboard display."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, meta_json, created_at, updated_at FROM proxy_sessions "
            "ORDER BY updated_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                **json.loads(r["meta_json"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
