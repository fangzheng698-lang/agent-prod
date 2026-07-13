# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Phase 4.3: CrossSessionMemory — 跨会话记忆。

用户在不同 session 之间的偏好/事实/习惯能被新 session 继承。
基于 SQLite，支持 LRU 淘汰，支持从消息中自动提取记忆。

用法:
    mem = CrossSessionMemory(":memory:")
    mem.store("user_prefers_concise", "true")
    system_prefix = mem.to_system_prompt_prefix()
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

try:
    import structlog
    _logger = structlog.get_logger("memory")
    _STRUCTLOG = True
except ImportError:
    _logger = logging.getLogger("memory")
    _STRUCTLOG = False

# 触发自动记忆提取的关键词
_MEMORY_TRIGGERS = [
    r"记住[我你]?[喜欢|偏好|想要|希望|总是|以后都]",
    r"以后都",
    r"不要[再|再给我]",
    r"我[喜欢|偏好|讨厌|不想]",
    r"别再",
    r"永远[不要|别]",
    r"don't\s+(ever\s+)?",
    r"remember\s+(that\s+)?",
]


class CrossSessionMemory:
    """轻量跨会话记忆，基于 SQLite。

    线程安全：SQLite WAL 模式，单写者多读者。
    """

    def __init__(self, db_path: str = ":memory:", max_entries: int = 50):
        self._max_entries = max_entries
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                access_count INTEGER DEFAULT 0,
                last_accessed_at TEXT DEFAULT ''
            )
        """)
        self._conn.commit()

    def store(self, key: str, value: str, category: str = "general") -> None:
        """存储一条记忆（upsert）。超过上限时 LRU 淘汰。"""
        now = datetime.now(UTC).isoformat()

        existing = self._conn.execute(
            "SELECT key FROM user_memory WHERE key = ?", (key,)
        ).fetchone()

        if existing:
            self._conn.execute(
                "UPDATE user_memory SET value = ?, category = ?, updated_at = ? WHERE key = ?",
                (value, category, now, key),
            )
        else:
            # LRU: 超上限时删除最老的一条
            count = self._conn.execute("SELECT COUNT(*) FROM user_memory").fetchone()[0]
            if count >= self._max_entries:
                oldest = self._conn.execute(
                    "SELECT key FROM user_memory ORDER BY last_accessed_at ASC NULLS FIRST LIMIT 1"
                ).fetchone()
                if oldest:
                    self._conn.execute("DELETE FROM user_memory WHERE key = ?", (oldest["key"],))
                    if _STRUCTLOG:
                        _logger.info(event="memory_evicted", key=oldest["key"], reason="lru_limit")

            self._conn.execute(
                "INSERT INTO user_memory (key, value, category, created_at, updated_at, access_count, last_accessed_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (key, value, category, now, now, now),
            )

        self._conn.commit()

    def get(self, key: str) -> str | None:
        """查询一条记忆，并更新访问次数和时间。"""
        row = self._conn.execute(
            "SELECT value FROM user_memory WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None

        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "UPDATE user_memory SET access_count = access_count + 1, last_accessed_at = ? WHERE key = ?",
            (now, key),
        )
        self._conn.commit()
        return row["value"]

    def get_all(self) -> list[dict[str, Any]]:
        """获取全部记忆。"""
        rows = self._conn.execute(
            "SELECT key, value, category, created_at, updated_at, access_count, last_accessed_at "
            "FROM user_memory ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_category(self, category: str) -> list[dict[str, Any]]:
        """按分类筛选记忆。"""
        rows = self._conn.execute(
            "SELECT key, value, category, updated_at FROM user_memory WHERE category = ? "
            "ORDER BY updated_at DESC",
            (category,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, key: str) -> None:
        """删除一条记忆。"""
        self._conn.execute("DELETE FROM user_memory WHERE key = ?", (key,))
        self._conn.commit()

    def to_system_prompt_prefix(self) -> str:
        """生成可注入 system prompt 的记忆前缀。

        格式:
            [PERSISTENT MEMORY]
            - user_prefers_concise: true
            - project_language: Python 3.11
        """
        rows = self._conn.execute(
            "SELECT key, value FROM user_memory ORDER BY updated_at DESC"
        ).fetchall()
        if not rows:
            return ""

        lines = ["[PERSISTENT MEMORY]"]
        for r in rows:
            lines.append(f"- {r['key']}: {r['value']}")
        return "\n".join(lines)

    def extract_from_messages(self, messages: list[dict[str, Any]]) -> int:
        """从消息中提取用户偏好关键字，自动存储。

        返回新增的记忆数量。
        """
        extracted = 0
        for msg in messages:
            if msg.get("role") != "user":
                continue
            text = msg.get("content", "")
            for pattern in _MEMORY_TRIGGERS:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    # 提取关键事实
                    snippet = text[m.start():m.end() + 100]
                    key = f"auto_{_auto_key(extracted)}"
                    self.store(key, snippet.strip(), "auto_extracted")
                    extracted += 1
                    break  # 一条消息只提取一条
        if extracted > 0 and _STRUCTLOG:
            _logger.info(event="memory_extracted", count=extracted)
        return extracted

    def close(self):
        self._conn.close()


def _auto_key(n: int) -> str:
    """生成唯一 auto key。

    用 secrets.token_hex(4) 生成 8 字符随机串（密码学安全），
    不再用 md5(time.time()) —— 后者可预测且微秒窗口内同 n 会碰撞。
    """
    import secrets
    return secrets.token_hex(4)
