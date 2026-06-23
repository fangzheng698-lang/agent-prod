"""
持久化层 — Improvement 的存储与查询
Phase 1: FileRepository 单机可用，PostgresRepository 生产多实例
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .models import Improvement

logger = logging.getLogger(__name__)


class ImprovementRepository(ABC):
    """改进项持久化抽象接口"""

    @abstractmethod
    def save(self, improvement: Improvement) -> None:
        """保存或更新 improvement"""
        ...

    @abstractmethod
    def get(self, improvement_id: str) -> Optional[Improvement]:
        """按 ID 查询"""
        ...

    @abstractmethod
    def list(self, status: Optional[str] = None,
             limit: int = 100, offset: int = 0) -> list[Improvement]:
        """列出改进项，可按状态过滤"""
        ...

    @abstractmethod
    def delete(self, improvement_id: str) -> None:
        """删除改进项"""
        ...

    @abstractmethod
    def count(self, status: Optional[str] = None) -> int:
        """按状态计数"""
        ...


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
        self._load()

    def save(self, improvement: Improvement) -> None:
        improvement.mark_updated()
        self._cache[improvement.id] = improvement
        self._persist()

    def get(self, improvement_id: str) -> Optional[Improvement]:
        return self._cache.get(improvement_id)

    def list(self, status: Optional[str] = None,
             limit: int = 100, offset: int = 0) -> list[Improvement]:
        results = list(self._cache.values())
        if status:
            results = [i for i in results if i.status.value == status]
        results.sort(key=lambda x: x.created_at, reverse=True)
        return results[offset:offset + limit]

    def delete(self, improvement_id: str) -> None:
        self._cache.pop(improvement_id, None)
        self._persist()

    def count(self, status: Optional[str] = None) -> int:
        if not status:
            return len(self._cache)
        return sum(1 for i in self._cache.values() if i.status.value == status)

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            data = json.loads(self.file_path.read_text())
            self._cache = {
                k: Improvement.model_validate(v)
                for k, v in data.items()
            }
            logger.info("Loaded %d improvements from %s",
                        len(self._cache), self.file_path)
        except Exception as e:
            logger.error("Failed to load improvements file: %s", e)
            self._cache = {}

    def _persist(self) -> None:
        try:
            data = {
                k: v.model_dump(mode='json')
                for k, v in self._cache.items()
            }
            self.file_path.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to persist improvements: %s", e)


class PostgresRepository(ImprovementRepository):
    """
    PostgreSQL 持久化 — 生产多实例使用
    需要数据库表结构:
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

    def __init__(self, dsn: str = "postgresql://user:pass@localhost:5432/quality_gates",
                 pool_size: int = 5):
        self.dsn = dsn
        self.pool_size = pool_size
        self._pool = None

    async def _ensure_pool(self):
        """惰性初始化连接池"""
        if self._pool is None:
            try:
                from asyncpg import create_pool
                self._pool = await create_pool(
                    dsn=self.dsn,
                    min_size=1,
                    max_size=self.pool_size,
                )
            except ImportError:
                raise RuntimeError(
                    "asyncpg is required for PostgresRepository. "
                    "Install: pip install asyncpg"
                )

    async def save(self, improvement: Improvement) -> None:
        improvement.mark_updated()
        await self._ensure_pool()
        async with self._pool.acquire() as conn:  # type: ignore
            await conn.execute(
                """
                INSERT INTO improvements (id, data, status, created_at, updated_at)
                VALUES ($1, $2::jsonb, $3, $4, $5)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at
                """,
                improvement.id,
                json.dumps(improvement.model_dump(mode='json')),
                improvement.status.value,
                improvement.created_at,
                improvement.updated_at,
            )

    async def get(self, improvement_id: str) -> Optional[Improvement]:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM improvements WHERE id = $1",
                improvement_id,
            )
            if row:
                import json as _json
                data = _json.loads(row['data']) if isinstance(row['data'], str) else row['data']
                return Improvement.model_validate(data)
            return None

    async def list(self, status: Optional[str] = None,
                    limit: int = 100, offset: int = 0) -> list[Improvement]:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT data FROM improvements WHERE status = $1 "
                    "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                    status, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    "SELECT data FROM improvements "
                    "ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                    limit, offset,
                )
            import json as _json
            return [
                Improvement.model_validate(
                    _json.loads(r['data']) if isinstance(r['data'], str) else r['data']
                )
                for r in rows
            ]

    async def delete(self, improvement_id: str) -> None:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM improvements WHERE id = $1",
                improvement_id,
            )

    async def count(self, status: Optional[str] = None) -> int:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            if status:
                row = await conn.fetchval(
                    "SELECT COUNT(*) FROM improvements WHERE status = $1",
                    status,
                )
            else:
                row = await conn.fetchval("SELECT COUNT(*) FROM improvements")
            return row or 0


class MemoryRepository(ImprovementRepository):
    """内存存储 — 测试和演示用"""

    def __init__(self):
        self._store: dict[str, Improvement] = {}
        self._save_count = 0

    def save(self, improvement: Improvement) -> None:
        improvement.mark_updated()
        self._store[improvement.id] = improvement
        self._save_count += 1

    def get(self, improvement_id: str) -> Optional[Improvement]:
        return self._store.get(improvement_id)

    def list(self, status: Optional[str] = None,
             limit: int = 100, offset: int = 0) -> list[Improvement]:
        results = list(self._store.values())
        if status:
            results = [i for i in results if i.status.value == status]
        results.sort(key=lambda x: x.created_at, reverse=True)
        return results[offset:offset + limit]

    def delete(self, improvement_id: str) -> None:
        self._store.pop(improvement_id, None)

    def count(self, status: Optional[str] = None) -> int:
        if not status:
            return len(self._store)
        return sum(1 for i in self._store.values() if i.status.value == status)

    def clear(self) -> None:
        """清空全部（测试用）"""
        self._store.clear()

    @property
    def save_count(self) -> int:
        return self._save_count
