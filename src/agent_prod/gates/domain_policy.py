# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""领域策略引擎 — 行业维度的工具风险升级与合规声明校验。

设计原则:
  1. 风险等级**只能升、不能降** — 同一个 write_file 在通用场景是 elevated，
     在金融场景应当升级为 dangerous，但永远不能反向降级。
  2. 行业策略是**配置驱动的增量层**，不修改原 TOOL_RISK 表，
     通过 get_effective_risk() 在原 risk 上做单向升级。
  3. 合规声明校验：金融场景下 dangerous 工具必须携带 audit_trail 声明，
     能源场景下控制类工具必须携带 safety_certificate 声明，否则直接拒绝。
  4. 缺失 domain 元数据时退化为通用风险，不阻断主流程。

配置示例 (config.yaml):
  policy:
    domains:
      finance:
        tools:
          write_file:
            risk_override: dangerous        # 强制升级
            required_claim: FINRA_audit_trail
            audit_level: strict
          patch:
            risk_override: dangerous
            required_claim: FINRA_audit_trail
          delegate_task:
            required_claim: SAR_review_required
      energy:
        tools:
          terminal:
            required_claim: SCADA_safety_certificate
            audit_level: strict
          shell_exec:
            required_claim: SCADA_safety_certificate
            data_classification: critical_infrastructure
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .tool_risk import RiskLevel, get_risk, resolve_tool_name

logger = logging.getLogger(__name__)


class ViolationType(str, Enum):
    """领域策略违规类型"""
    DOMAIN_RISK_ESCALATION = "domain_risk_escalation"      # 风险已被升级（记录用，不阻断）
    MISSING_COMPLIANCE_CLAIM = "missing_compliance_claim"  # 缺少合规声明（阻断）
    INVALID_CLAIM_FORMAT = "invalid_claim_format"          # 合规声明格式错误（阻断）
    DATA_CLASSIFICATION_VIOLATION = "data_classification_violation"  # 数据分级违规


@dataclass
class DomainToolPolicy:
    """单工具在某个行业域内的策略"""
    risk_override: RiskLevel | None = None   # 强制风险等级（只能升不能降）
    required_claim: str = ""                  # 必须携带的合规声明 key
    audit_level: str = ""                     # 审计级别: strict | standard | light
    data_classification: str = ""             # 数据分级标签


@dataclass
class DomainPolicyResult:
    """领域策略查询结果"""
    effective_risk: RiskLevel
    base_risk: RiskLevel | None                # 原工具风险
    domain: str = ""
    tool: str = ""
    policy: DomainToolPolicy | None = None     # 命中的领域策略
    escalated: bool = False                     # 是否被升级
    violation: ViolationType | None = None
    violation_detail: str = ""


class DomainPolicyEngine:
    """行业策略引擎 — 加载并应用 config.yaml 的 policy.domains 段。

    提供两个核心能力:
      1. get_effective_risk(tool_name, agent_type, domain) -> DomainPolicyResult
         返回最终风险等级（只升不降） + 升级理由
      2. validate_compliance_claims(tool_name, domain, claims) -> DomainPolicyResult
         校验合规声明是否完整
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._domains: dict[str, dict[str, DomainToolPolicy]] = {}
        self._load_domains()

    def _load_domains(self) -> None:
        """从 config.policy.domains 加载每个行业的工具策略。"""
        policy_cfg = self._config.get("policy", {}) if self._config else {}
        domains_cfg = policy_cfg.get("domains", {}) or {}
        if not isinstance(domains_cfg, dict):
            logger.warning("policy.domains is not a dict, skipping domain policy load")
            return

        for domain_name, domain_cfg in domains_cfg.items():
            if not isinstance(domain_cfg, dict):
                continue
            tools_cfg = domain_cfg.get("tools", {}) or {}
            if not isinstance(tools_cfg, dict):
                continue
            self._domains[domain_name] = {}
            for tool_name, tool_cfg in tools_cfg.items():
                if not isinstance(tool_cfg, dict):
                    continue
                risk_str = tool_cfg.get("risk_override", "")
                risk_override: RiskLevel | None = None
                if risk_str:
                    try:
                        risk_override = RiskLevel(risk_str)
                    except ValueError:
                        logger.warning(
                            "Unknown risk_override '%s' for %s/%s, ignoring",
                            risk_str, domain_name, tool_name,
                        )
                self._domains[domain_name][tool_name] = DomainToolPolicy(
                    risk_override=risk_override,
                    required_claim=tool_cfg.get("required_claim", ""),
                    audit_level=tool_cfg.get("audit_level", ""),
                    data_classification=tool_cfg.get("data_classification", ""),
                )

        if self._domains:
            logger.info(
                "DomainPolicyEngine loaded %d domain(s): %s",
                len(self._domains), ", ".join(sorted(self._domains.keys())),
            )

    # ──────────────────────────────────────────────────────
    #  API
    # ──────────────────────────────────────────────────────

    def get_effective_risk(
        self,
        tool_name: str,
        agent_type: str | None = None,
        domain: str = "",
    ) -> DomainPolicyResult:
        """返回工具在指定行业下的有效风险等级。

        单向升级行为:
          - 如果 domain 没配置该工具，返回通用 risk
          - 如果 domain 配置了 risk_override，比通用 risk 高则升级，低则忽略
          - 跨级升级 (benign -> dangerous) 也是允许的
        """
        canonical = resolve_tool_name(tool_name, agent_type)
        base = get_risk(canonical, agent_type) or RiskLevel.ELEVATED  # 未知按 elevated 起算
        base_for_result = get_risk(canonical, agent_type)

        if not domain or domain not in self._domains:
            return DomainPolicyResult(
                effective_risk=base,
                base_risk=base_for_result,
                domain=domain,
                tool=canonical,
            )

        domain_tools = self._domains[domain]
        policy = domain_tools.get(canonical)
        if policy is None or policy.risk_override is None:
            return DomainPolicyResult(
                effective_risk=base,
                base_risk=base_for_result,
                domain=domain,
                tool=canonical,
                policy=policy,
            )

        override = policy.risk_override
        escalated = _severity(override) > _severity(base)
        if not escalated and override != base:
            # 风险降级不允许 — 沉默忽略 override
            logger.info(
                "Domain %s override %s for %s is lower than base %s, ignored",
                domain, override.value, canonical, base.value,
            )
            return DomainPolicyResult(
                effective_risk=base,
                base_risk=base_for_result,
                domain=domain,
                tool=canonical,
                policy=policy,
            )

        return DomainPolicyResult(
            effective_risk=override,
            base_risk=base_for_result,
            domain=domain,
            tool=canonical,
            policy=policy,
            escalated=escalated,
        )

    def validate_compliance_claims(
        self,
        tool_name: str,
        domain: str,
        claims: dict[str, Any] | None,
        agent_type: str | None = None,
    ) -> DomainPolicyResult:
        """校验工具在指定行业下是否携带必需的合规声明。

        claims 结构示例:
          {"FINRA_audit_trail": {"timestamp": "...", "actor": "..."},
           "SAR_review_required": True}
        """
        canonical = resolve_tool_name(tool_name, agent_type)
        effective = self.get_effective_risk(canonical, agent_type, domain)

        if not domain or domain not in self._domains:
            return effective

        domain_tools = self._domains[domain]
        policy = domain_tools.get(canonical)
        if policy is None or not policy.required_claim:
            return effective

        claims = claims or {}
        required_key = policy.required_claim
        if required_key not in claims:
            return DomainPolicyResult(
                effective_risk=effective.effective_risk,
                base_risk=effective.base_risk,
                domain=domain,
                tool=canonical,
                policy=policy,
                escalated=effective.escalated,
                violation=ViolationType.MISSING_COMPLIANCE_CLAIM,
                violation_detail=(
                    f"Tool '{canonical}' in domain '{domain}' requires "
                    f"compliance claim '{required_key}' but it was not provided"
                ),
            )

        claim_value = claims[required_key]
        if not _is_valid_claim(claim_value):
            return DomainPolicyResult(
                effective_risk=effective.effective_risk,
                base_risk=effective.base_risk,
                domain=domain,
                tool=canonical,
                policy=policy,
                escalated=effective.escalated,
                violation=ViolationType.INVALID_CLAIM_FORMAT,
                violation_detail=(
                    f"Compliance claim '{required_key}' for {canonical} "
                    f"in domain '{domain}' has invalid format"
                ),
            )

        return effective

    def list_domains(self) -> list[str]:
        """已配置的行业列表"""
        return sorted(self._domains.keys())

    def domain_tool_count(self, domain: str) -> int:
        """某行业下配置的工具数"""
        return len(self._domains.get(domain, {}))


# ──────────────────────────────────────────────────────
#  内部工具
# ──────────────────────────────────────────────────────

_SEVERITY = {
    RiskLevel.BENIGN: 0,
    RiskLevel.ELEVATED: 1,
    RiskLevel.DANGEROUS: 2,
}


def _severity(level: RiskLevel) -> int:
    return _SEVERITY.get(level, 1)


def _is_valid_claim(value: Any) -> bool:
    """合规声明格式校验 — 必须是 truthy 且非空字符串/字典。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return len(value.strip()) > 0
    if isinstance(value, dict):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return True
    return False
