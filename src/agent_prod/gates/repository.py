"""
持久化层 — Improvement 的存储与查询
Phase 1: FileRepository 单机可用，PostgresRepository 生产多实例
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from datetime import UTC
from functools import wraps
from pathlib import Path

from .models import Improvement

logger = logging.getLogger(__name__)


class ImprovementRepository(ABC):
    """改进项持久化抽象接口"""


    @abstractmethod
    def save(self, improvement: Improvement) -> None:
        """保存或更新 improvement"""
        ...

    @abstractmethod
    def get(self, improvement_id: str) -> Improvement | None:
        """按 ID 查询"""
        ...

    @abstractmethod
    def list(self, status: str | None = None,
             limit: int = 100, offset: int = 0) -> list[Improvement]:
        """列出改进项，可按状态过滤"""
        ...

    @abstractmethod
    def delete(self, improvement_id: str) -> None:
        """删除改进项"""
        ...

    @abstractmethod
    def count(self, status: str | None = None) -> int:
        """按状态计数"""
        ...


def _retry_on_oserror(max_attempts: int = 3, delay: float = 0.5):
    """Repository 写入重试装饰器 — 仅重试 OSError (磁盘满/权限等 transient)。"""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except OSError as e:
                    last_err = e
                    if attempt < max_attempts:
                        logger.warning(
                            "Repository %s failed (attempt %d/%d): %s",
                            fn.__name__, attempt, max_attempts, e,
                        )
                        time.sleep(delay * attempt)
                    else:
                        raise
            raise last_err  # type: ignore[misc]
        return wrapper
    return decorator


class FileRepository(ImprovementRepository):
    """
    文件持久化 — 单机可用，不依赖外部服务
    使用 JSON 文件存储，整表加载到内存
    适用于 10K 级别以下的 improvement 量
    """

    def __init__(self, file_path: str = "/var/lib/quality_gates/improvements.json"):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Improvement] = {}
        self._lock = threading.Lock()
        self._load()

    def save(self, improvement: Improvement) -> None:
        improvement.mark_updated()
        with self._lock:
            self._cache[improvement.id] = improvement
            data = self._build_snapshot()
        try:
            self._persist(data)
        except OSError as e:
            logger.error("Failed to persist improvements (OS): %s", e)

    def get(self, improvement_id: str) -> Improvement | None:
        with self._lock:
            return self._cache.get(improvement_id)

    def list(self, status: str | None = None,
             limit: int = 100, offset: int = 0) -> list[Improvement]:
        with self._lock:
            results = list(self._cache.values())
            if status:
                results = [i for i in results if i.status.value == status]
            results.sort(key=lambda x: x.created_at, reverse=True)
            return results[offset:offset + limit]

    def delete(self, improvement_id: str) -> None:
        with self._lock:
            self._cache.pop(improvement_id, None)
            data = self._build_snapshot()
        try:
            self._persist(data)
        except OSError as e:
            logger.error("Failed to persist delete (OS): %s", e)

    def count(self, status: str | None = None) -> int:
        with self._lock:
            if not status:
                return len(self._cache)
            return sum(1 for i in self._cache.values() if i.status.value == status)

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            data = json.loads(self.file_path.read_text())
            self._cache = {}
            for k, v in data.items():
                imp = Improvement.model_validate(v)
                # 规范化时区：model_validate 可能产生混合 aware/naive datetime，
                # 统一转为 UTC-aware 避免排序时报错
                if imp.created_at.tzinfo is None:
                    imp.created_at = imp.created_at.replace(tzinfo=UTC)
                if imp.updated_at and imp.updated_at.tzinfo is None:
                    imp.updated_at = imp.updated_at.replace(tzinfo=UTC)
                self._cache[k] = imp
            logger.info("Loaded %d improvements from %s",
                        len(self._cache), self.file_path)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.error("Failed to parse improvements file (%s): %s", type(e).__name__, e)
            self._cache = {}
        except OSError as e:
            logger.error("Failed to read improvements file (%s): %s", type(e).__name__, e)
            self._cache = {}

    def _build_snapshot(self) -> bytes:
        """Serialize current cache to JSON bytes. Must be called under lock."""
        data = {
            k: v.model_dump(mode='json')
            for k, v in self._cache.items()
        }
        return json.dumps(data, indent=2, default=str).encode('utf-8')

    @_retry_on_oserror(max_attempts=3, delay=0.5)
    def _persist(self, data: bytes) -> None:
        """Atomic write via temp-file + rename — retries on transient OSError.

        Writes to a .tmp file first, then atomically renames over the
        target.  If the process is killed mid-write, the original file
        is intact (or the .tmp is orphaned, which is harmless).

        Args:
            data: Pre-serialized JSON bytes to write.
        """
        tmp = self.file_path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, self.file_path)

    def _persist_full(self) -> None:
        """Full persist (serialize + write). Used externally after bulk changes."""
        with self._lock:
            data = self._build_snapshot()
        self._persist(data)


class PostgresRepository(ImprovementRepository):
    """
    PostgreSQL 持久化 — 生产多实例使用 (sync via psycopg2).

    表结构:
      CREATE TABLE improvements (
          id TEXT PRIMARY KEY,
          data JSONB NOT NULL,
          status TEXT NOT NULL DEFAULT 'candidate',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      );
      CREATE INDEX idx_improvements_status ON improvements(status);
      CREATE INDEX idx_improvements_created_at ON improvements(created_at);
    """

    def __init__(self, dsn: str = "",
                 pool_size: int = 5):
        import psycopg2
        import psycopg2.pool
        from urllib.parse import urlparse

        self.dsn = dsn
        self._pool = None
        self._pool_size = max(1, pool_size)

        # Parse minimal connect kwargs from DSN
        if dsn and "://" in dsn:
            u = urlparse(dsn)
            self._conn_kwargs = {
                "host": u.hostname or "localhost",
                "port": u.port or 5432,
                "dbname": u.path.lstrip("/") or "quality_gates",
                "user": u.username or "quality_gates",
                "password": u.password or "",
            }
        else:
            self._conn_kwargs = {
                "host": "localhost",
                "port": 5432,
                "dbname": "quality_gates",
                "user": "quality_gates",
                "password": "",
            }

    def _ensure_pool(self):
        if self._pool is None:
            import psycopg2.pool
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=self._pool_size, **self._conn_kwargs,
            )

    def save(self, improvement: Improvement) -> None:
        self._ensure_pool()
        improvement.mark_updated()
        data_json = json.dumps(improvement.model_dump(mode='json'))
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO improvements (id, data, status, created_at, updated_at)
                    VALUES (%s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        data = EXCLUDED.data,
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (improvement.id, data_json, improvement.status.value,
                     improvement.created_at, improvement.updated_at),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get(self, improvement_id: str) -> Improvement | None:
        self._ensure_pool()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM improvements WHERE id = %s",
                    (improvement_id,),
                )
                row = cur.fetchone()
                if row:
                    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    imp = Improvement.model_validate(data)
                    if imp.created_at.tzinfo is None:
                        from datetime import UTC
                        imp.created_at = imp.created_at.replace(tzinfo=UTC)
                    if imp.updated_at and imp.updated_at.tzinfo is None:
                        from datetime import UTC
                        imp.updated_at = imp.updated_at.replace(tzinfo=UTC)
                    return imp
                return None
        finally:
            self._pool.putconn(conn)

    def list(self, status: str | None = None,
             limit: int = 100, offset: int = 0) -> list[Improvement]:
        self._ensure_pool()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        "SELECT data FROM improvements WHERE status = %s "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        (status, limit, offset),
                    )
                else:
                    cur.execute(
                        "SELECT data FROM improvements "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        (limit, offset),
                    )
                from datetime import UTC
                results = []
                for (row_data,) in cur:
                    data = row_data if isinstance(row_data, dict) else json.loads(row_data)
                    imp = Improvement.model_validate(data)
                    if imp.created_at.tzinfo is None:
                        imp.created_at = imp.created_at.replace(tzinfo=UTC)
                    if imp.updated_at and imp.updated_at.tzinfo is None:
                        imp.updated_at = imp.updated_at.replace(tzinfo=UTC)
                    results.append(imp)
                return results
        finally:
            self._pool.putconn(conn)

    def delete(self, improvement_id: str) -> None:
        self._ensure_pool()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM improvements WHERE id = %s",
                    (improvement_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def count(self, status: str | None = None) -> int:
        self._ensure_pool()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        "SELECT COUNT(*) FROM improvements WHERE status = %s",
                        (status,),
                    )
                else:
                    cur.execute("SELECT COUNT(*) FROM improvements")
                return cur.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        if self._pool:
            self._pool.closeall()
            self._pool = None


class MemoryRepository(ImprovementRepository):
    """内存存储 — 测试和演示用"""

    def __init__(self):
        self._store: dict[str, Improvement] = {}
        self._save_count = 0

    def save(self, improvement: Improvement) -> None:
        improvement.mark_updated()
        self._store[improvement.id] = improvement
        self._save_count += 1

    def get(self, improvement_id: str) -> Improvement | None:
        return self._store.get(improvement_id)

    def list(self, status: str | None = None,
             limit: int = 100, offset: int = 0) -> list[Improvement]:
        results = list(self._store.values())
        if status:
            results = [i for i in results if i.status.value == status]
        results.sort(key=lambda x: x.created_at, reverse=True)
        return results[offset:offset + limit]

    def delete(self, improvement_id: str) -> None:
        self._store.pop(improvement_id, None)

    def count(self, status: str | None = None) -> int:
        if not status:
            return len(self._store)
        return sum(1 for i in self._store.values() if i.status.value == status)

    def clear(self) -> None:
        """清空全部（测试用）"""
        self._store.clear()

    @property
    def save_count(self) -> int:
        return self._save_count
