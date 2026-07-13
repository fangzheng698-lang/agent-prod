"""
Gate7: 执行一致性门 — 验证子 agent 的产出是否与分配的计划一致。

核心功能:
  1. expected_plan vs final_response 对比（语义级，LLM 判断）
  2. expected_tool_names vs actual tool_calls 对比（工具调用级）
  3. 输出偏差报告：未完成的任务、多余的操作、工具调用不匹配

依赖:
  - improvement.candidate_output["expected_plan"] — 主 agent 分配的任务描述
  - improvement.candidate_output["final_response"] — 子 agent 的实际回复
  - improvement.metadata.get("expected_tool_names") — 预期工具列表（可选）
  - improvement.tool_calls — 实际工具调用记录
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from .interface import GatePlugin, register_gate
from .models import GateName, GateResult, Improvement, RollbackLevel, RollbackPlan
from .reasoning import EvidenceSource, EvidenceType, ReasoningStep

logger = logging.getLogger(__name__)

try:
    from deepdiff import DeepDiff
    _DEEPDIFF_AVAILABLE = True
except ImportError:
    _DEEPDIFF_AVAILABLE = False
    DeepDiff = None  # type: ignore


def apply_gate7_reasoning(improvement, result, deviations, mode):
    """向推理链追加 Gate7 决策记录"""
    improvement.init_reasoning_chain()
    evidence = [
        EvidenceSource(
            type=EvidenceType.PATTERN_MATCH,
            name="plan_consistency",
            value={
                "total_deviations": len(deviations),
                "critical": sum(1 for d in deviations if d.get("severity") == "critical"),
                "warning": sum(1 for d in deviations if d.get("severity") == "warning"),
                "mode": mode,
            },
            confidence=0.9,
        ),
    ]
    for d in deviations[:3]:
        evidence.append(EvidenceSource(
            type=EvidenceType.PATTERN_MATCH,
            name=d.get("type", "deviation"),
            value={"detail": d.get("detail", "")[:200]},
            confidence=0.85,
        ))
    step = ReasoningStep(
        step_id=f"g7-{uuid.uuid4().hex[:8]}",
        gate="gate7",
        decision="PASS" if result.passed else "FAIL",
        reason=result.reason,
        evidence=evidence,
        confidence=0.9,
    )
    improvement.reasoning_chain.add_step(step)


class Gate7ExecutionConsistency(GatePlugin):
    """执行一致性门 — 计划 vs 实际对比


    支持 observe/enforce 双模式（与 Gate0 一致）：
      - observe（默认）: 发现偏离只记录不阻断，passed=True
      - enforce: 发现 critical 偏离直接拒绝
    """

    name = GateName.GATE7
    rollback_level = RollbackLevel.L1

    def __init__(self, config=None, raw_config=None, repository=None):
        self.config = config or {}
        self._raw_config = raw_config
        self._repo = repository

        # ── 模式解析（同 Gate0 风格） ──
        gate7_cfg = (raw_config or {}).get("gates", {}).get("gate7", {}) if raw_config else {}
        self._global_mode = gate7_cfg.get("mode", "observe")  # 默认 observe
        self._per_agent_modes: dict[str, str] = {}
        per_agent = gate7_cfg.get("per_agent", {})
        if isinstance(per_agent, dict):
            for agent, cfg in per_agent.items():
                if isinstance(cfg, dict) and "mode" in cfg:
                    self._per_agent_modes[agent] = cfg["mode"]

    def _resolve_mode(self, agent: str, metadata_override: str | None = None) -> str:
        """解析 agent 生效模式：metadata override > per_agent > global > 默认 observe"""
        if metadata_override:
            return metadata_override
        return self._per_agent_modes.get(agent, self._global_mode)

    def verify(self, improvement: Improvement) -> GateResult:
        start = time.time()
        candidate = improvement.candidate_output or {}
        metadata = improvement.metadata or {}

        expected_plan = candidate.get("expected_plan", "").strip()
        final_response = candidate.get("final_response", "").strip()

        # 如果没有 expected_plan，跳过（不是被分配任务的情况）
        if not expected_plan:
            result = GateResult(
                gate_name=GateName.GATE7,
                passed=True,
                reason="No expected_plan — skipping plan consistency check",
                details={"skipped": True, "reason": "no_expected_plan"},
                duration_ms=(time.time() - start) * 1000,
            )
            apply_gate7_reasoning(improvement, result, [], mode="observe")
            return result

        deviations = []

        # ── 1. expected_plan vs final_response 语义对比 ──
        plan_issues = self._compare_plan_vs_response(expected_plan, final_response)
        deviations.extend(plan_issues)

        # ── 2. 预期工具 vs 实际工具调用对比（如果有） ──
        expected_tools = metadata.get("expected_tool_names", [])
        if expected_tools:
            actual_tools = self._collect_actual_tool_names(improvement)
            tool_issues = self._compare_tools(expected_tools, actual_tools)
            deviations.extend(tool_issues)

        # ── 3. tool_calls 数量合理性检查 ──
        plan_len = len(expected_plan)
        tool_calls = improvement.tool_calls or []
        if plan_len > 100 and len(tool_calls) == 0 and len(final_response) < 50:
            deviations.append({
                "type": "no_effort",
                "field": "tool_calls",
                "detail": "No tool calls despite substantial plan — likely no real work done",
                "severity": "critical",
            })

        # ── 判定 ──
        critical = [d for d in deviations if d.get("severity") == "critical"]
        warnings = [d for d in deviations if d.get("severity") == "warning"]

        mode = self._resolve_mode(
            metadata.get("agent", ""),
            metadata_override=metadata.get("gate7_mode"),
        )
        if mode == "observe":
            # 观察者模式：发现偏离只记录不阻断
            passed = True
        else:
            # enforce 模式：有 critical 偏离就拒绝
            passed = len(critical) == 0

        details = {
            "expected_plan_snippet": expected_plan[:200],
            "final_response_snippet": final_response[:200],
            "deviation_count": len(deviations),
            "critical_count": len(critical),
            "warning_count": len(warnings),
            "deviations": deviations,
            "mode": mode,
        }

        reason_parts = []
        if mode == "observe" and deviations:
            reason_parts.append(f"[OBSERVE] {len(deviations)} deviation(s) detected — logged only")
        elif mode == "observe":
            reason_parts.append("[OBSERVE] Executed as planned")
        if critical:
            reason_parts.append(f"{len(critical)} critical")
        if warnings:
            reason_parts.append(f"{len(warnings)} warnings")
        if not reason_parts:
            reason_parts.append("Executed as planned")

        result = GateResult(
            gate_name=GateName.GATE7,
            passed=passed,
            reason="; ".join(reason_parts),
            details=details,
            duration_ms=(time.time() - start) * 1000,
        )
        apply_gate7_reasoning(improvement, result, deviations, mode=mode)
        return result

    def _compare_plan_vs_response(self, plan: str, response: str) -> list[dict]:
        """对计划 vs 回复做简单文本级对比。

        使用关键词重叠率和长度比做初步过滤，标记明显偏离。
        精确判断交给 Gate6 的 follows_plan 维度（LLM）。
        """
        deviations = []

        # 空回复 / 极短回复
        if not response or len(response) < 20:
            deviations.append({
                "type": "empty_response",
                "field": "final_response",
                "detail": f"Response too short ({len(response)} chars) for a substantial plan",
                "severity": "critical",
            })
            return deviations

        # 检查回复中是否包含计划中的核心动词/名词
        import re
        plan_keywords = self._extract_keywords(plan)
        if plan_keywords:
            matched = sum(1 for kw in plan_keywords if kw.lower() in response.lower())
            match_ratio = matched / len(plan_keywords)
            if match_ratio < 0.15:
                deviations.append({
                    "type": "topic_divergence",
                    "field": "final_response",
                    "detail": (f"Response shares only {matched}/{len(plan_keywords)} "
                               f"({match_ratio:.0%}) keywords with plan — likely off-topic"),
                    "severity": "critical",
                })
            elif match_ratio < 0.4:
                deviations.append({
                    "type": "partial_coverage",
                    "field": "final_response",
                    "detail": (f"Response covers only {matched}/{len(plan_keywords)} "
                               f"({match_ratio:.0%}) planned keywords — may be incomplete"),
                    "severity": "warning",
                })

        return deviations

    def _collect_actual_tool_names(self, improvement: Improvement) -> set[str]:
        """收集 improvement 中所有实际使用的工具名称。"""
        tools = set()

        # 顶层 tool_calls
        for tc in (improvement.tool_calls or []):
            name = getattr(tc, "tool_name", None) or (isinstance(tc, dict) and tc.get("tool_name"))
            if name:
                tools.add(str(name))

        # decisions 中的 tool_calls
        for dec in (improvement.llm_calls or []):
            for tc in (getattr(dec, "tool_calls", None) or []):
                name = getattr(tc, "tool_name", None) or (isinstance(tc, dict) and tc.get("tool_name"))
                if name:
                    tools.add(str(name))

        return tools

    def _compare_tools(self, expected: list[str], actual: set[str]) -> list[dict]:
        """对比预期工具和实际工具。"""
        deviations = []
        expected_set = set(expected)
        expected_lower = {e.lower() for e in expected}

        missing = expected_set - actual
        # 检查大小写不敏感的缺失
        if not missing:
            actual_lower = {a.lower() for a in actual}
            missing = {e for e in expected if e.lower() not in actual_lower}

        if missing:
            deviations.append({
                "type": "missing_tools",
                "field": "tool_calls",
                "detail": f"Expected tools not used: {', '.join(sorted(missing))}",
                "severity": "warning",
            })

        return deviations

    @staticmethod
    def _extract_keywords(text: str, max_keywords: int = 15) -> list[str]:
        """从文本中提取关键名词/动词短语。

        提取策略：取长度 >= 2 的中文/英文单词，过滤停用词。
        """
        import re
        # 提取中文词组（2-6 字）
        cn_words = re.findall(r'[一-鿿]{2,6}', text)
        # 提取英文单词（长度 >= 4）
        en_words = re.findall(r'[a-zA-Z]{4,}', text)

        stopwords = {"this", "that", "with", "from", "have", "been", "will",
                     "would", "could", "should", "their", "there", "which",
                     "what", "about", "into", "than", "then", "also", "note"}

        keywords = cn_words + [w.lower() for w in en_words if w.lower() not in stopwords]
        # 去重并限制数量
        seen = set()
        unique = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique[:max_keywords]

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L1 回滚：清除 candidate_output 中的 expected_plan 标记"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L1,
            scope="clear expected_plan from candidate_output",
            estimated_seconds=5,
            procedure="REMOVE candidate_output.expected_plan",
            executed_at=None,
            success=False,
        )

    @classmethod
    def from_config(cls, config: dict, name: GateName) -> Gate7ExecutionConsistency:
        return cls(config=None, raw_config=config)


# ── GatePlugin registration ──────────────────────────────
register_gate(GateName.GATE7, Gate7ExecutionConsistency)
