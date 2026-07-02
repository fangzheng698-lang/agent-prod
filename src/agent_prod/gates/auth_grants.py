# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""授权记录存储 — 用户对 agent 危险操作的显式授权。

授权模型: 用户为特定的 (agent_type, tool_name) 颁发授权。
授权有过期时间，过期自动失效。

API:
  POST   /v1/auth/grant   — 颁发授权
  GET    /v1/auth/grants   — 列出有效授权
  DELETE /v1/auth/grant/{id} — 撤销授权
"""

from __future__ import annotations

import logging
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认授权文件路径: 环境变量 > ~/.agent-prod/auth_grants.json
_DEFAULT_AUTH_GRANTS_PATH = os.environ.get(
    "AUTH_GRANTS_PATH",
    str(Path.home() / ".agent-prod" / "auth_grants.json"),
)


@dataclass
class AuthGrant:
    grant_id: str
    agent_type: str
    tool_name: str
    granted_by: str       # 谁颁发的 ("alice", "admin")
    reason: str           # 为什么授权
    issued_at: float = field(default_factory=time.time)
    expires_at: float = float("inf")  # 0 = never, >0 = unix timestamp
    revoked: bool = False

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        if self.expires_at <= time.time():
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "agent_type": self.agent_type,
            "tool_name": self.tool_name,
            "granted_by": self.granted_by,
            "reason": self.reason,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at if self.expires_at != float("inf") else 0,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AuthGrant:
        return cls(
            grant_id=d["grant_id"],
            agent_type=d["agent_type"],
            tool_name=d["tool_name"],
            granted_by=d["granted_by"],
            reason=d["reason"],
            issued_at=d["issued_at"],
            expires_at=d.get("expires_at", float("inf")) or float("inf"),
            revoked=d.get("revoked", False),
        )


class AuthGrantStore:
    """内存 + 文件持久化的授权存储。

    写入 /var/lib/quality_gates/auth_grants.json
    """

    def __init__(self, file_path: str | None = None):
        self._file_path = Path(file_path or _DEFAULT_AUTH_GRANTS_PATH)
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._grants: dict[str, AuthGrant] = {}
        self._load()

    def _load(self) -> None:
        if not self._file_path.exists():
            return
        try:
            data = json.loads(self._file_path.read_text()) or {}
            for gid, gd in data.items():
                grant = AuthGrant.from_dict(gd)
                if grant.is_valid():
                    self._grants[gid] = grant
        except Exception as e:
            logger.warning("Failed to load auth grants: %s", e)

    def _save(self) -> None:
        data = {gid: g.to_dict() for gid, g in self._grants.items()}
        tmp = self._file_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        tmp.rename(self._file_path)

    def grant(self, agent_type: str, tool_name: str,
              granted_by: str, reason: str = "",
              ttl_seconds: float = 0) -> AuthGrant:
        gid = f"auth-{uuid.uuid4().hex[:8]}"
        expires = time.time() + ttl_seconds if ttl_seconds > 0 else float("inf")
        grant = AuthGrant(
            grant_id=gid,
            agent_type=agent_type,
            tool_name=tool_name,
            granted_by=granted_by,
            reason=reason,
            expires_at=expires,
        )
        self._grants[gid] = grant
        self._save()
        logger.info("Auth grant issued: %s %s/%s → %s", gid, agent_type, tool_name, granted_by)
        return grant

    def check(self, agent_type: str, tool_name: str) -> AuthGrant | None:
        """检查 agent 对 tool 是否有有效授权。"""
        for grant in self._grants.values():
            if not grant.is_valid():
                continue
            if grant.agent_type == agent_type and grant.tool_name == tool_name:
                return grant
        return None

    def check_by_id(self, grant_id: str) -> AuthGrant | None:
        """按 grant_id 精确匹配。"""
        g = self._grants.get(grant_id)
        if g and g.is_valid():
            return g
        return None

    def revoke(self, grant_id: str) -> bool:
        g = self._grants.get(grant_id)
        if g:
            g.revoked = True
            self._save()
            return True
        return False

    def list_valid(self, agent_type: str = "") -> list[AuthGrant]:
        result = []
        for g in self._grants.values():
            if not g.is_valid():
                continue
            if agent_type and g.agent_type != agent_type:
                continue
            result.append(g)
        return result
