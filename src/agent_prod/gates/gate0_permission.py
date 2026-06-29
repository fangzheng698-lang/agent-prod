"""Gate0: 权限准入 — 风险分级 + 声明校验 + 授权检查 + 参数安全检测。

三层判断逻辑:

  Layer A 工具风险分类:
    benign:    只记录，不拦
    elevated:  需在 declared_tools 中声明，否则拒
    dangerous: 需有有效授权，否则拦+告警
    unknown:   不在风险库中的工具 → 拦

  Layer B 参数安全检测 (对抗 LLM 语义绕过):
    即使工具名合法，参数可能包含恶意意图。
    Level 1: 确定性拦截 — 写入 /etc/passwd, curl | sh 等
    Level 2: 可疑标记 — 需 LLM 二次审查
    Level 3: LLM 意图分类 (可选)

  Agent 可声明工具列表 (declared_tools)，Gate0 校验行为偏离。
  受信 agent (trusted_agents) 免授权但全部记录。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .models import GateName, GateResult, Improvement
from .tool_risk import RiskLevel, auto_classify_tool, get_risk, is_known_tool
from .argument_inspection import check_tool_call, ThreatLevel
from .intent_classifier import rule_based_classify

logger = logging.getLogger(__name__)


class Gate0Permission:
    """风险分级权限准入门。

    从 config.yaml gate0 段加载 auth_grant_store 和阈值配置。
    """

    def __init__(self, config: dict | None = None, auth_store=None):
        self._config = config or {}
        self._auth_store = auth_store  # AuthGrantStore | None
        gate0_cfg = self._config.get("gates", {}).get("gate0", {})
        self._block_unknown = gate0_cfg.get("block_unknown_tools", True)
        self._skip_arg_inspection = gate0_cfg.get("skip_arg_inspection", False)
        # 观察者/拦截模式
        self._global_mode = gate0_cfg.get("mode", "enforce")  # "enforce" | "observe"
        self._per_agent_modes: dict[str, str] = {}
        per_agent = gate0_cfg.get("per_agent", {})
        if isinstance(per_agent, dict):
            for agent, cfg in per_agent.items():
                if isinstance(cfg, dict) and "mode" in cfg:
                    self._per_agent_modes[agent] = cfg["mode"]

        # ── LLM 配置 (复用 gate6 的 LLM 配置用于工具自动分类) ──
        gate6_cfg = self._config.get("gates", {}).get("gate6", {})
        self._llm_config: dict | None = None
        if gate6_cfg:
            self._llm_config = {
                "llm_endpoint": gate6_cfg.get("llm_endpoint", ""),
                "llm_model": gate6_cfg.get("llm_model", ""),
                "llm_api_key": gate6_cfg.get("llm_api_key", ""),
                "timeout_seconds": gate6_cfg.get("timeout_seconds", 10.0),
            }

    def _resolve_mode(self, agent: str) -> str:
        """返回 agent 当前生效的运行模式。"""
        return self._per_agent_modes.get(agent, self._global_mode)

    def set_mode(self, agent: str, mode: str) -> dict:
        """热切换 agent 的运行模式。agent="" 切换全局默认。

        返回当前所有生效的配置。
        """
        valid_modes = {"enforce", "observe"}
        if mode not in valid_modes:
            raise ValueError(f"无效模式 '{mode}'，有效值: {valid_modes}")
        if agent:
            self._per_agent_modes[agent] = mode
        else:
            self._global_mode = mode
        return self.get_mode(agent or "__all__")

    def get_mode(self, agent: str = "") -> dict:
        """查询当前模式配置。

        agent="" 返回全局默认 + 所有 per-agent 覆盖。
        agent="hermes" 返回 hermes 实际生效的模式。
        """
        if agent:
            return {
                "agent": agent,
                "effective_mode": self._resolve_mode(agent),
                "source": "per_agent" if agent in self._per_agent_modes else "global",
            }
        return {
            "global_mode": self._global_mode,
            "per_agent": dict(self._per_agent_modes),
        }

    @classmethod
    def from_yaml(cls, config: dict | None,
                  auth_store=None) -> Gate0Permission:
        return cls(config=config, auth_store=auth_store)

    # ═══════════════════════════════════════════════════════════
    #  验证逻辑
    # ═══════════════════════════════════════════════════════════

    def verify(self, improvement: Improvement) -> GateResult:
        t0 = time.time()

        agent = improvement.metadata.get("agent", "generic")
        declared: list[str] = improvement.metadata.get("declared_tools", [])
        declared_set = set(declared)
        auth_grant_id: str = improvement.metadata.get("auth_grant_id", "")

        decisions = improvement.metadata.get("decisions", [])
        total_calls = 0

        # 收集所有 tool_calls (含参数)
        all_tools: list[dict[str, Any]] = []
        for d in decisions:
            for tc in d.get("tool_calls", []):
                total_calls += 1
                all_tools.append({
                    "tool": tc.get("tool_name", ""),
                    "decision_id": d.get("decision_id", "?"),
                    "arguments": tc.get("arguments", {}),
                })

        # 分类判定
        passes: list[dict] = []
        blocks: list[dict] = []
        elevated_logs: list[dict] = []
        dangerous_logs: list[dict] = []

        for tc in all_tools:
            tool = tc["tool"]
            risk = get_risk(tool, agent)

            # ── 未知工具 → 尝试 LLM 自动分类 ──
            if risk is None:
                classified = auto_classify_tool(tool, agent, llm_config=self._llm_config)
                if classified is not None:
                    canonical_name, risk = classified
                    logger.info("Gate0 auto-classified %s/%s as %s (%s)",
                                 agent, tool, canonical_name, risk.value)
                elif self._block_unknown:
                    blocks.append({
                        **tc,
                        "type": "unknown_tool",
                        "reason": f"工具 '{tool}' 不在已知风险库中",
                    })
                    continue
                else:
                    # 宽松模式：放行但记录
                    dangerous_logs.append({
                        **tc, "risk": "unknown", "note": "unknown tool, logged only"
                    })
                    continue

            # ── benign → 记录放行 ──
            if risk == RiskLevel.BENIGN:
                passes.append({**tc, "risk": "benign"})
                continue

            # ── elevated → 需声明 ──
            if risk == RiskLevel.ELEVATED:
                if tool in declared_set:
                    elevated_logs.append({
                        **tc, "risk": "elevated", "declared": True
                    })
                else:
                    blocks.append({
                        **tc,
                        "type": "undeclared_elevated",
                        "reason": (f"工具 '{tool}' 是 elevated 级别，"
                                   f"但未在 declared_tools 中声明"),
                    })
                continue

            # ── dangerous → 需授权 ──
            if risk == RiskLevel.DANGEROUS:
                authorized = False
                auth_source = ""

                # 1) 检查 auth_grant_id
                if auth_grant_id and self._auth_store:
                    grant = self._auth_store.check_by_id(auth_grant_id)
                    if grant and grant.agent_type == agent and grant.tool_name == tool:
                        authorized = True
                        auth_source = f"grant:{auth_grant_id}"

                # 2) 检查 auth_store 中是否有该 (agent, tool) 的有效授权
                if not authorized and self._auth_store:
                    grant = self._auth_store.check(agent, tool)
                    if grant:
                        authorized = True
                        auth_source = f"grant:{grant.grant_id}"

                if authorized:
                    dangerous_logs.append({
                        **tc, "risk": "dangerous",
                        "authorized": True, "auth_source": auth_source,
                    })
                else:
                    blocks.append({
                        **tc,
                        "type": "unauthorized_dangerous",
                        "reason": (f"工具 '{tool}' 是 dangerous 级别，"
                                   f"需要用户显式授权"),
                    })

        # ═══════════════════════════════════════════════════════════
        #  Layer B: 参数安全检测 (对抗 LLM 语义绕过)
        # ═══════════════════════════════════════════════════════════
        #  Layer B: 参数安全检测 (对抗 LLM 语义绕过)
        # ── 可通过 skip_arg_inspection 配置关闭 ──
        arg_blocks: list[dict] = []
        arg_flagged: list[dict] = []

        if not self._skip_arg_inspection:
            # 检查 benign 类（正常不应有危险参数，但做保险）
            for tc in passes:
                result = check_tool_call(tc["tool"], tc.get("arguments", {}))
                if result.threat == ThreatLevel.BLOCK:
                    arg_blocks.append({**tc, "type": "arg_blocked",
                        "reason": f"参数安全检查拦截: {result.reason}"})
                elif result.threat == ThreatLevel.SUSPICIOUS:
                    arg_flagged.append({**tc, "type": "arg_suspicious",
                        "reason": result.reason})

            # 检查 elevated 类 — 重点防御（write_file 写系统路径等）
            for tc in elevated_logs:
                result = check_tool_call(tc["tool"], tc.get("arguments", {}))
                if result.threat == ThreatLevel.BLOCK:
                    arg_blocks.append({**tc, "type": "arg_blocked_elevated",
                        "reason": f"参数安全检查拦截: {result.reason}"})
                elif result.threat == ThreatLevel.SUSPICIOUS:
                    arg_flagged.append({**tc, "type": "arg_suspicious_elevated",
                        "reason": result.reason})

            # 检查 dangerous 类 — 即使已授权也做参数检测
            for tc in dangerous_logs:
                result = check_tool_call(tc["tool"], tc.get("arguments", {}))
                if result.threat == ThreatLevel.BLOCK:
                    arg_blocks.append({**tc, "type": "arg_blocked_dangerous",
                        "reason": f"参数安全检查拦截: {result.reason}"})
                elif result.threat == ThreatLevel.SUSPICIOUS:
                    arg_flagged.append({**tc, "type": "arg_suspicious_dangerous",
                        "reason": result.reason})

        # 合并参数拦截到 blocks
        if arg_blocks:
            blocks.extend(arg_blocks)

        duration_ms = (time.time() - t0) * 1000

        # ── 汇总结果 ──
        details = {
            "agent": agent,
            "total_tool_calls": total_calls,
            "declared_tools": declared,
            "benign_passed": len(passes),
            "elevated_logged": len(elevated_logs),
            "dangerous_authorized": len(dangerous_logs),
            "arg_suspicious": len(arg_flagged),
            "arg_flagged_tools": [f["tool"] for f in arg_flagged],
            "blocked": len(blocks),
            "passes": [p["tool"] for p in passes],
            "elevated": [e["tool"] for e in elevated_logs],
            "dangerous": [d["tool"] for d in dangerous_logs],
            "violations": blocks,
        }

        # ── 模式判定 ──
        mode = self._resolve_mode(agent)
        details["mode"] = mode

        if blocks:
            violation_tools = [b["tool"] for b in blocks]
            if mode == "observe":
                # 观察者模式：记录违规但不拦截
                return GateResult(
                    gate_name=GateName.GATE0,
                    passed=True,
                    reason=(f"[OBSERVE] 检测到 {len(blocks)} 次违规但不拦截: "
                            f"{violation_tools}"),
                    details=details,
                    duration_ms=duration_ms,
                )
            return GateResult(
                gate_name=GateName.GATE0,
                passed=False,
                reason=(f"权限拒绝: agent '{agent}' 的 {len(blocks)} 次工具调用被拦截: "
                        f"{violation_tools}"),
                details=details,
                duration_ms=duration_ms,
            )

        return GateResult(
            gate_name=GateName.GATE0,
            passed=True,
            reason=(f"权限检查通过: agent '{agent}', {total_calls} 次调用 "
                    f"(benign={len(passes)} elevated={len(elevated_logs)} "
                    f"dangerous={len(dangerous_logs)} blocked=0)"),
            details=details,
            duration_ms=duration_ms,
        )

    def rollback(self, improvement: Improvement) -> None:
        pass

    # ═══════════════════════════════════════════════════════════
    #  运行时单次工具准入 (方案 A: Gateway 工具代理)
    # ═══════════════════════════════════════════════════════════

    def check_single_tool(
        self,
        agent: str,
        tool_name: str,
        arguments: dict | None = None,
        declared_tools: list[str] | None = None,
        auth_grant_id: str = "",
    ) -> dict:
        """对单次工具调用做 Gate0 准入检查，返回 {allowed, reason, risk, ...}。

        用于 /v1/tool/execute 端点 —— agent 的工具调用
        在真正执行前先过核心安全检查。
        """
        declared_set = set(declared_tools or [])
        args = arguments or {}
        result: dict[str, Any] = {
            "allowed": False,
            "tool": tool_name,
            "agent": agent,
            "risk": "unknown",
            "reason": "",
            "auth_source": "",
        }

        risk = get_risk(tool_name, agent)

        # ── 未知工具 → 尝试 LLM 自动分类 ──
        if risk is None:
            classified = auto_classify_tool(tool_name, agent, llm_config=self._llm_config)
            if classified is not None:
                canonical_name, risk = classified
                logger.info("Gate0 auto-classified %s/%s as %s (%s)",
                             agent, tool_name, canonical_name, risk.value)
            elif self._block_unknown:
                result["reason"] = f"工具 '{tool_name}' 不在已知风险库中"
                return result
            else:
                result["risk"] = "unknown"
                result["allowed"] = True
                return result

        result["risk"] = risk.value

        # benign: 放行（但仍做参数安全检测，SUSPICIOUS 标记不阻断）
        if risk == RiskLevel.BENIGN:
            arg_result = check_tool_call(tool_name, args)
            result["allowed"] = True
            if arg_result.threat == ThreatLevel.BLOCK:
                result["allowed"] = False
                result["reason"] = f"参数安全检查拦截: {arg_result.reason}"
                result["arg_threat"] = "block"
                return result
            if arg_result.threat == ThreatLevel.SUSPICIOUS:
                result["arg_threat"] = "suspicious"
                result["arg_reason"] = arg_result.reason
            return result

        # elevated: 需声明
        if risk == RiskLevel.ELEVATED:
            if tool_name not in declared_set:
                result["reason"] = (
                    f"工具 '{tool_name}' 是 elevated 级别，"
                    f"但未在 declared_tools 中声明"
                )
                return result

        # dangerous: 需授权
        if risk == RiskLevel.DANGEROUS:
            authorized = False
            # 1) auth_grant_id
            if auth_grant_id and self._auth_store:
                grant = self._auth_store.check_by_id(auth_grant_id)
                if grant and grant.agent_type == agent and grant.tool_name == tool_name:
                    authorized = True
                    result["auth_source"] = f"grant:{auth_grant_id}"
            # 2) (agent, tool) 有效授权
            if not authorized and self._auth_store:
                grant = self._auth_store.check(agent, tool_name)
                if grant:
                    authorized = True
                    result["auth_source"] = f"grant:{grant.grant_id}"

            if not authorized:
                result["reason"] = (
                    f"工具 '{tool_name}' 是 dangerous 级别，"
                    f"需要用户显式授权"
                )
                return result

        # ── Layer B: 参数安全检测 ──
        arg_result = check_tool_call(tool_name, args)
        if arg_result.threat == ThreatLevel.BLOCK:
            result["allowed"] = False
            result["reason"] = f"参数安全检查拦截: {arg_result.reason}"
            result["arg_threat"] = "block"
            return result
        if arg_result.threat == ThreatLevel.SUSPICIOUS:
            result["arg_threat"] = "suspicious"
            result["arg_reason"] = arg_result.reason

        result["allowed"] = True
        return result
