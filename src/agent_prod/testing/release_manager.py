"""Phase 6.4: Release Manager — 发布状态管理。

管理发布流程: candidate → gray_1% → gray_10% → gray_50% → production。
支持任意阶段回滚到 rolled_back。

用法:
    rm = ReleaseManager()
    rm.create_release("v1.0.0", "imp-001", "Initial release")
    rm.promote("v1.0.0")  # candidate → gray_1%
    rm.promote("v1.0.0")  # gray_1% → gray_10%
    rm.rollback("v1.0.0") # any → rolled_back
    prod = rm.get_production()
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ReleaseStatus(str, Enum):
    """发布状态枚举。"""
    CANDIDATE = "candidate"
    GRAY_1 = "gray_1%"
    GRAY_10 = "gray_10%"
    GRAY_50 = "gray_50%"
    PRODUCTION = "production"
    ROLLED_BACK = "rolled_back"


# ── 状态流转表 ──
_PROMOTE_ORDER: list[ReleaseStatus] = [
    ReleaseStatus.CANDIDATE,
    ReleaseStatus.GRAY_1,
    ReleaseStatus.GRAY_10,
    ReleaseStatus.GRAY_50,
    ReleaseStatus.PRODUCTION,
]


class StatusTransition(BaseModel):
    """一次状态变更记录。"""
    from_status: ReleaseStatus
    to_status: ReleaseStatus
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    reason: str = ""


class ReleaseState(BaseModel):
    """发布状态记录。"""
    version: str
    status: ReleaseStatus = ReleaseStatus.CANDIDATE
    improvement_id: str = ""
    notes: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    history: list[StatusTransition] = Field(default_factory=list)


class ReleaseManager:
    """发布管理器。

    管理多个版本的发布状态，支持 promote / rollback / status 查询。

    线程安全：单线程使用，无需锁。
    """

    def __init__(self):
        self._releases: dict[str, ReleaseState] = {}

    # ── CRUD ──

    def create_release(
        self,
        version: str,
        improvement_id: str,
        notes: str = "",
    ) -> ReleaseState:
        """创建一个新的发布候选。

        参数:
            version: 版本号 (如 "v1.0.0")
            improvement_id: 关联的 Improvement ID
            notes: 发布说明

        返回:
            创建的 ReleaseState

        异常:
            ValueError: 版本号已存在
        """
        if version in self._releases:
            raise ValueError(f"Version '{version}' already exists")

        now = datetime.now(timezone.utc).isoformat()
        state = ReleaseState(
            version=version,
            status=ReleaseStatus.CANDIDATE,
            improvement_id=improvement_id,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        # 记录初始状态
        state.history.append(StatusTransition(
            from_status=ReleaseStatus.CANDIDATE,
            to_status=ReleaseStatus.CANDIDATE,
            reason="Release created",
        ))
        self._releases[version] = state
        return state

    def status(self, version: str) -> Optional[ReleaseState]:
        """查询版本发布状态。"""
        return self._releases.get(version)

    def get_production(self) -> Optional[ReleaseState]:
        """获取当前 production 版本（最近到达 production 的版本）。"""
        prod_releases = [
            r for r in self._releases.values()
            if r.status == ReleaseStatus.PRODUCTION
        ]
        if not prod_releases:
            return None
        # 返回最近更新的 production 版本
        return max(prod_releases, key=lambda r: r.updated_at)

    def list_releases(
        self,
        status: Optional[ReleaseStatus] = None,
    ) -> list[ReleaseState]:
        """列出所有发布，可按状态过滤。"""
        results = list(self._releases.values())
        if status:
            results = [r for r in results if r.status == status]
        # 按创建时间倒序
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results

    # ── 状态流转 ──

    def promote(self, version: str, reason: str = "") -> ReleaseState:
        """将发布推进到下一个阶段。

        参数:
            version: 版本号
            reason: 推进原因（可选）

        返回:
            更新后的 ReleaseState

        异常:
            ValueError: 版本不存在或状态不允许推进
        """
        state = self._releases.get(version)
        if state is None:
            raise ValueError(f"Version '{version}' not found")

        current = state.status

        # 已回滚的不能推进
        if current == ReleaseStatus.ROLLED_BACK:
            raise ValueError(
                f"Cannot promote rolled_back release '{version}'"
            )

        # 已在 production 则保持
        if current == ReleaseStatus.PRODUCTION:
            return state

        # 找到下一阶段
        try:
            idx = _PROMOTE_ORDER.index(current)
            next_status = _PROMOTE_ORDER[idx + 1]
        except (ValueError, IndexError):
            # 未知状态或已到最后
            return state

        return self._transition(state, next_status, reason)

    def rollback(self, version: str, reason: str = "") -> ReleaseState:
        """回滚发布到 rolled_back 状态。

        参数:
            version: 版本号
            reason: 回滚原因（可选）

        返回:
            更新后的 ReleaseState

        异常:
            ValueError: 版本不存在
        """
        state = self._releases.get(version)
        if state is None:
            raise ValueError(f"Version '{version}' not found")

        # 如果已经回滚过，仍然可以再次标记
        return self._transition(state, ReleaseStatus.ROLLED_BACK, reason or "Rollback requested")

    # ── 内部 ──

    def _transition(
        self,
        state: ReleaseState,
        to_status: ReleaseStatus,
        reason: str = "",
    ) -> ReleaseState:
        """执行状态转换并记录历史。"""
        from_status = state.status

        # 如果状态相同且不是初始创建，不需要重复记录
        if from_status == to_status and state.history:
            return state

        now = datetime.now(timezone.utc).isoformat()
        state.status = to_status
        state.updated_at = now
        state.history.append(StatusTransition(
            from_status=from_status,
            to_status=to_status,
            timestamp=now,
            reason=reason,
        ))

        return state
