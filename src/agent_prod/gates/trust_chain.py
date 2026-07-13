# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""多 Agent 信任链 — 子 Agent 权限继承与作用域限制。

设计原则:
  1. 子 Agent 继承父 Agent 的 declared_tools 子集，不能越出父权限
  2. trust_level 控制继承幅度: full | restricted | sandbox
  3. 正向声明: 子 Agent 的 allowed_tools 必须显式声明，不使用"黑名单"模式
  4. 缺失信任链记录时后退到通用 Gate0 权限检查（不额外限制）

使用场景:
  - 主 Agent (qclaw) 派生子 Agent (code-reviewer) → child 只能 read_file/search_files
  - 主 Agent (qclaw) 给子 Agent (malicious-agent) → 即使 child 声明了 dangerous 工具也受限
  - 多级嵌套: parent -> child -> grandchild 作用域只减不增
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from .tool_risk import RiskLevel, get_risk, resolve_tool_name

logger = logging.getLogger(__name__)


class TrustLevel(str, Enum):
    """信任等级"""
    FULL = "full"              # 继承父全部可用工具，等同于自我管理
    RESTRICTED = "restricted"  # 只能使用父显式授予的工具（默认）
    SANDBOX = "sandbox"        # 最大限制：只读 + 无副作用，超时自动失效


@dataclass
class TaskACL:
    """单次任务委托的权限控制记录"""
    task_id: str
    parent_agent: str
    child_agent: str
    trust_level: TrustLevel = TrustLevel.RESTRICTED
    allowed_tools: set[str] = field(default_factory=set)   # 子可用的规范工具名集合
    allowed_domains: set[str] = field(default_factory=set)  # 子可用的行业域
    data_scope: str = ""          # 数据范围描述（如 "project:agent-prod"）
    expires_at: datetime | None = None  # 过期时间，None 表示不过期
    parent_auth_grant_id: str = ""     # 父 Agent 的授权 ID（用于追溯）

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_agent": self.parent_agent,
            "child_agent": self.child_agent,
            "trust_level": self.trust_level.value,
            "allowed_tools": sorted(self.allowed_tools),
            "allowed_domains": sorted(self.allowed_domains),
            "data_scope": self.data_scope,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "expired": self.is_expired,
        }


class TrustChainError(Exception):
    """信任链验证失败"""
    pass


class TrustChainValidator:
    """信任链验证器 — 检查子 Agent 是否越权。

    Gate0 集成点:
      当 improvement.metadata 包含 parent_agent 时，Gate0.verify()
      应调用 TrustChainValidator.validate_tool_scope() 额外检查。

    Gate7 集成点:
      Gate7 检查 expected_plan 的来源时，对比 parent 的 expected_plan
      与 child 的实际回复，验证委托关系真实发生。
    """

    def __init__(self):
        self._acls: dict[str, TaskACL] = {}  # task_id -> ACL
        self._child_acls: dict[str, list[str]] = {}  # child_agent -> [task_ids]

    # ── ACL 管理 ──

    def register_task(self, acl: TaskACL) -> None:
        """注册一个任务委托的 ACL"""
        self._acls[acl.task_id] = acl
        self._child_acls.setdefault(acl.child_agent, []).append(acl.task_id)
        logger.info(
            "TrustChain: task %s registered — parent=%s child=%s level=%s tools=%s",
            acl.task_id, acl.parent_agent, acl.child_agent,
            acl.trust_level.value, sorted(acl.allowed_tools),
        )

    def get_acl(self, task_id: str) -> TaskACL | None:
        return self._acls.get(task_id)

    def get_child_acls(self, child_agent: str) -> list[TaskACL]:
        return [
            self._acls[tid] for tid in self._child_acls.get(child_agent, [])
            if tid in self._acls
        ]

    def get_parent_acls(self, parent_agent: str) -> list[TaskACL]:
        return [
            acl for acl in self._acls.values()
            if acl.parent_agent == parent_agent
        ]

    # ── 验证方法 ──

    def validate_tool_scope(
        self,
        tool_name: str,
        child_agent: str,
        agent_type: str | None = None,
        task_id: str | None = None,
    ) -> tuple[bool, str]:
        """检查子 Agent 的工具调用是否在 ACL 允许范围内。

        Args:
            tool_name: 工具名（支持别名解析）
            child_agent: 子 Agent 类型
            agent_type: 用于解析别名的原始 agent_type
            task_id: 如果提供，只检查该任务

        Returns:
            (allowed: bool, reason: str)
        """
        canonical = resolve_tool_name(tool_name, agent_type or child_agent)

        # 收集该 child 的所有 ACL
        acls = self.get_child_acls(child_agent)
        if task_id:
            acls = [a for a in acls if a.task_id == task_id]
        if not acls:
            # 无信任链记录 → 不额外限制（退回到通用 Gate0 检查）
            return True, "no trust chain records for this child"

        # 过滤掉已过期的 ACL（过期 = 不再生效，等同于不存在）
        active_acls = [a for a in acls if not a.is_expired]
        if not active_acls:
            return True, "all ACLs expired — no active restriction"

        # 对所有有效 ACL 逐一检查（并集：任一允许即放行）
        for acl in active_acls:
            if acl.trust_level == TrustLevel.FULL:
                return True, "full trust — no tool restriction"

            if acl.trust_level == TrustLevel.SANDBOX:
                # sandbox 模式：只允许 read_file 类
                tool_risk = get_risk(canonical, agent_type or child_agent)
                if tool_risk == RiskLevel.BENIGN:
                    return True, f"sandbox: benign tool {canonical} allowed"
                return False, (
                    f"sandbox: {canonical} ({tool_risk.value if tool_risk else 'unknown'}) "
                    "not allowed in sandbox mode"
                )

            # RESTRICTED: 必须在 allowed_tools 中
            if canonical in acl.allowed_tools:
                return True, (
                    f"restricted trust: {canonical} in allowed_tools"
                )

        # 没有任何 ACL 允许该工具
        return False, (
            f"tool '{canonical}' not in any ACL allowed_tools "
            f"for child '{child_agent}'"
        )

    def validate_domain_scope(
        self,
        domain: str,
        child_agent: str,
    ) -> tuple[bool, str]:
        """检查子 Agent 使用的行业域是否在 ACL 范围内。"""
        # 没有 ACL 不做额外限制
        acls = self.get_child_acls(child_agent)
        if not acls:
            return True, "no trust chain records — domain allowed"

        for acl in acls:
            if acl.is_expired:
                continue
            if acl.trust_level == TrustLevel.FULL:
                return True, "full trust — domain unrestricted"
            if not acl.allowed_domains:
                continue  # 空列表 = 不限域
            if domain in acl.allowed_domains:
                return True, f"domain '{domain}' in allowed_domains"

        return False, (
            f"domain '{domain}' not in any ACL allowed_domains "
            f"for child '{child_agent}'"
        )

    def validate_parent_plan_match(
        self,
        parent_agent: str | None,
        parent_plan: str,
        child_reply: str,
    ) -> tuple[bool, str]:
        """Gate7 集成：检查子 Agent 的回复是否对应父的确切计划。"""
        if not parent_agent or not parent_plan:
            return True, "no parent context — skip plan match"

        # 简单的文本相似度检查
        # 不要求精确一致，但至少不能完全不相关
        if not child_reply:
            return False, f"child replied empty despite parent plan from {parent_agent}"

        # 检查计划中的核心关键词是否出现在回复中
        from .gate7_execution_consistency import Gate7ExecutionConsistency
        keywords = Gate7ExecutionConsistency._extract_keywords(parent_plan)
        if not keywords:
            return True, "plan has no extractable keywords — skip"

        matched = sum(1 for kw in keywords if kw.lower() in child_reply.lower())
        ratio = matched / len(keywords)
        if ratio >= 0.2:
            return True, (
                f"parent plan matched: {matched}/{len(keywords)} keywords "
                f"({ratio:.0%}) in child reply"
            )
        return False, (
            f"parent plan mismatch: only {matched}/{len(keywords)} keywords "
            f"({ratio:.0%}) in child reply — child may not be executing parent's plan"
        )
