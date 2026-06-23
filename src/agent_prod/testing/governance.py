"""Phase 7.4: Governance — 治理面板。

提供发布状态的文本化视图，包括灰度状态、候选版本列表和回滚历史。

用法:
    from agent_prod.testing.governance import GovernancePanel

    panel = GovernancePanel(release_manager)
    status = panel.get_gray_status()
    candidates = panel.list_candidates()
    history = panel.rollback_history()
    print(panel.to_text())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# 延迟导入避免循环依赖
ReleaseManager = None  # type: ignore


def _get_release_manager():
    """延迟导入 ReleaseManager。"""
    global ReleaseManager
    if ReleaseManager is None:
        from agent_prod.testing.release_manager import (
            ReleaseManager as RM,
            ReleaseStatus,
            ReleaseState,
            StatusTransition,
        )
        ReleaseManager = RM
        return RM, ReleaseStatus, ReleaseState, StatusTransition
    from agent_prod.testing.release_manager import (
        ReleaseStatus,
        ReleaseState,
        StatusTransition,
    )
    return ReleaseManager, ReleaseStatus, ReleaseState, StatusTransition


@dataclass
class RollbackRecord:
    """回滚操作记录。"""
    version: str
    from_status: str
    to_status: str = "rolled_back"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    reason: str = ""


class GovernancePanel:
    """治理面板：以纯文本形式呈现发布状态和控制面。

    依赖 ReleaseManager 提供数据源。
    """

    def __init__(self, release_manager=None):
        """
        参数:
            release_manager: ReleaseManager 实例。如果为 None，自动创建。
        """
        if release_manager is None:
            _RM, _, _, _ = _get_release_manager()
            release_manager = _RM()
        self._rm = release_manager

    def get_gray_status(self) -> dict[str, Any]:
        """获取各灰度阶段的版本分布。

        返回:
            {
                "candidate": list[str],    # 候选版本列表
                "gray_1pct": list[str],    # 1% 灰度版本
                "gray_10pct": list[str],   # 10% 灰度版本
                "gray_50pct": list[str],   # 50% 灰度版本
                "production": list[str],   # 生产版本
            }
        """
        _, ReleaseStatus, _, _ = _get_release_manager()

        status_map = {
            "candidate": ReleaseStatus.CANDIDATE,
            "gray_1pct": ReleaseStatus.GRAY_1,
            "gray_10pct": ReleaseStatus.GRAY_10,
            "gray_50pct": ReleaseStatus.GRAY_50,
            "production": ReleaseStatus.PRODUCTION,
        }

        result: dict[str, list[str]] = {}
        for key, status_enum in status_map.items():
            releases = self._rm.list_releases(status=status_enum)
            result[key] = [r.version for r in releases]

        return result

    def list_candidates(self) -> list[str]:
        """列出所有候选版本 (candidate 状态)。

        返回:
            版本号字符串列表
        """
        _, ReleaseStatus, _, _ = _get_release_manager()
        releases = self._rm.list_releases(status=ReleaseStatus.CANDIDATE)
        return [r.version for r in releases]

    def rollback_history(self) -> list[RollbackRecord]:
        """获取所有回滚记录。

        遍历所有版本的历史转换，提取回滚事件。

        返回:
            RollbackRecord 列表
        """
        _, ReleaseStatus, _, _ = _get_release_manager()

        records: list[RollbackRecord] = []
        all_releases = self._rm.list_releases()

        for release in all_releases:
            for transition in release.history:
                if transition.to_status == ReleaseStatus.ROLLED_BACK:
                    records.append(RollbackRecord(
                        version=release.version,
                        from_status=transition.from_status.value,
                        to_status=transition.to_status.value,
                        timestamp=transition.timestamp,
                        reason=transition.reason,
                    ))

        # 按时间倒序
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records

    def to_text(self) -> str:
        """生成纯文本治理面板。

        返回:
            格式化的多行文本面板
        """
        lines = []
        lines.append("=" * 60)
        lines.append("  Governance Panel")
        lines.append("=" * 60)
        lines.append("")

        # ── 灰度状态 ──
        lines.append("── Gray Release Status ──")
        gray = self.get_gray_status()
        labels = [
            ("Candidate", "candidate"),
            ("Gray 1%", "gray_1pct"),
            ("Gray 10%", "gray_10pct"),
            ("Gray 50%", "gray_50pct"),
            ("Production", "production"),
        ]
        for label, key in labels:
            versions = gray.get(key, [])
            count = len(versions)
            bar = "█" * min(count, 20)
            if versions:
                lines.append(f"  {label:>12}: {bar} ({count})  {', '.join(versions)}")
            else:
                lines.append(f"  {label:>12}: (empty)")

        lines.append("")

        # ── 候选版本 ──
        lines.append("── Candidate Versions ──")
        candidates = self.list_candidates()
        if candidates:
            for i, v in enumerate(candidates, 1):
                lines.append(f"  {i}. {v}")
        else:
            lines.append("  (none)")

        lines.append("")

        # ── 回滚历史 ──
        lines.append("── Rollback History ──")
        rollbacks = self.rollback_history()
        if rollbacks:
            for i, rb in enumerate(rollbacks, 1):
                lines.append(
                    f"  {i}. {rb.version}: {rb.from_status} → {rb.to_status} "
                    f"({rb.timestamp[:19]}) — {rb.reason}"
                )
        else:
            lines.append("  (no rollbacks)")

        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        """生成治理摘要（结构化数据）。

        返回:
            {
                "total_releases": int,
                "candidate_count": int,
                "production_count": int,
                "rollback_count": int,
                "gray_status": dict,
            }
        """
        gray = self.get_gray_status()
        rollbacks = self.rollback_history()
        all_releases = self._rm.list_releases()
        return {
            "total_releases": len(all_releases),
            "candidate_count": len(gray.get("candidate", [])),
            "production_count": len(gray.get("production", [])),
            "rollback_count": len(rollbacks),
            "gray_status": gray,
        }
