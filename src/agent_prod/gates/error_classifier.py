"""Gate6 错误分类器 — 将答案不匹配归入可操作的错误类别。

当 Gate6 检测到 score < threshold 时，ErrorClassifier 分析
candidate_output vs expected_answer，将错误分为：

    fact_error      — 事实错误（巴黎是德国的）
    format_error    — 格式不匹配（列表 vs 段落）
    omission        — 遗漏关键信息（应该是 A+B，只给了 A）
    hallucination   — 编造了不存在的细节
    precision       — 不够精确（答案对但太笼统）
    unknown         — 无法分类

输出 ErrorClass 枚举 + 置信度 + 修复建议。
"""

from __future__ import annotations

import re
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class ErrorClass(str, Enum):
    FACT_ERROR = "fact_error"
    FORMAT_ERROR = "format_error"
    OMISSION = "omission"
    HALLUCINATION = "hallucination"
    PRECISION = "precision"
    UNKNOWN = "unknown"


@dataclass
class ErrorClassification:
    error_class: ErrorClass = ErrorClass.UNKNOWN
    confidence: float = 0.0
    reason: str = ""
    evidence: str = ""          # 引用原文证据
    fix_suggestion: str = ""
    sub_errors: list[dict[str, Any]] = field(default_factory=list)


class ErrorClassifier:
    """答案错误分类器 — 纯规则引擎，零外部依赖.

    设计原则：
    - 规则驱动，确定性分类
    - 复合错误 → 优先级: fact_error > hallucination > omission > precision > format_error
    - 置信度评估基于证据强度
    """

    # ── 事实词库 ────────────────────────────────────
    _NEGATION_WORDS = {"不是", "不是的", "不", "没有", "无", "错了", "不对", "错误",
                        "并非", "非"}
    _CERTAINTY_MARKERS = {"一定", "肯定", "绝对", "必须", "总是", "永远",
                           "毫无疑问", "显然", "众所周知"}

    # 高置信事实模式 — 包含否定词 + 确定性词
    _HIGH_CONFIDENCE_FACT_PATTERN = re.compile(
        r"(?:是|为|在|位于|属于|等于|包含|有)(.*?)(?:的|了|。|，|！|\n|$)"
    )

    @staticmethod
    def classify(
        candidate: str,
        expected: str,
        score: float = 0.0,
    ) -> ErrorClassification:
        """分析 candidate 与 expected 的差异，归类错误."""
        c = str(candidate).strip()
        e = str(expected).strip()

        if not c or not e:
            return ErrorClassification(
                error_class=ErrorClass.UNKNOWN,
                confidence=0.0,
                reason="候选或期望答案为空",
            )

        # 收集所有证据
        findings: list[dict[str, Any]] = []

        # 1) 事实错误检测
        fact_finding = _detect_fact_error(c, e)
        if fact_finding:
            findings.append(fact_finding)

        # 2) 幻觉检测
        halluc_finding = _detect_hallucination(c, e)
        if halluc_finding:
            findings.append(halluc_finding)

        # 3) 遗漏检测
        omission_finding = _detect_omission(c, e)
        if omission_finding:
            findings.append(omission_finding)

        # 4) 精确度检测
        precision_finding = _detect_precision_issue(c, e)
        if precision_finding:
            findings.append(precision_finding)

        # 5) 格式错误
        format_finding = _detect_format_error(c, e)
        if format_finding:
            findings.append(format_finding)

        if not findings:
            return ErrorClassification(
                error_class=ErrorClass.UNKNOWN,
                confidence=0.3,
                reason=f"无法自动分类 — score={score:.3f}",
                fix_suggestion="人工审核答案差异",
            )

        # 取最高优先级
        priority_order = [
            ErrorClass.FACT_ERROR,
            ErrorClass.HALLUCINATION,
            ErrorClass.OMISSION,
            ErrorClass.PRECISION,
            ErrorClass.FORMAT_ERROR,
        ]
        findings.sort(key=lambda f: (
            priority_order.index(f["error_class"]) if f["error_class"] in priority_order else 99,
            -f["confidence"],
        ))
        best = findings[0]

        # 证据组合
        evidence_lines = []
        for f in findings[:3]:
            evidence_lines.append(f"[{f['error_class'].value}] {f['reason']}")
            if f.get("example"):
                evidence_lines.append(f"  例: {f['example']}")

        return ErrorClassification(
            error_class=best["error_class"],
            confidence=best["confidence"],
            reason=best["reason"],
            evidence="\n".join(evidence_lines),
            fix_suggestion=_suggest_fix(best["error_class"]),
            sub_errors=findings,
        )


# ── 检测器 ──────────────────────────────────────────

def _detect_fact_error(candidate: str, expected: str) -> dict | None:
    """事实错误：包含关键否定/确定性陈述且与期望矛盾."""
    c_lower = candidate.lower()
    e_lower = expected.lower()

    # 法 1: 关键词否定检测
    c_words = set(_tokenize(candidate))
    e_words = set(_tokenize(expected))

    c_has_negation = bool(c_words & ErrorClassifier._NEGATION_WORDS)
    e_has_negation = bool(e_words & ErrorClassifier._NEGATION_WORDS)

    if c_has_negation and not e_has_negation:
        return {
            "error_class": ErrorClass.FACT_ERROR,
            "confidence": 0.75,
            "reason": "候选包含否定词而期望不含 — 可能为事实性错误",
            "example": _truncate_diff(candidate, expected),
        }

    # 法 2: 核心实体矛盾
    # 提取数字化信息对比
    c_nums = set(re.findall(r'\b\d+\b', candidate))
    e_nums = set(re.findall(r'\b\d+\b', expected))
    if c_nums and e_nums and c_nums != e_nums:
        return {
            "error_class": ErrorClass.FACT_ERROR,
            "confidence": 0.85,
            "reason": f"数字信息矛盾: 候选 {c_nums} vs 期望 {e_nums}",
            "example": f"候选数值: {c_nums} | 期望数值: {e_nums}",
        }

    # 法 3: 长度比极大（可能是完全不同的答案）
    if len(candidate) > 0 and len(expected) > 0:
        ratio = min(len(candidate), len(expected)) / max(len(candidate), len(expected))
        if ratio < 0.3:
            return {
                "error_class": ErrorClass.FACT_ERROR,
                "confidence": 0.6,
                "reason": f"答案长度差异巨大 (ratio={ratio:.2f})",
                "example": f"候选({len(candidate)}字): {_trunc(candidate, 80)}\n期望({len(expected)}字): {_trunc(expected, 80)}",
            }

    return None


def _detect_hallucination(candidate: str, expected: str) -> dict | None:
    """幻觉：候选包含了期望中没有的细节，且有确定性标记."""
    c_words = set(_tokenize(candidate))
    e_words = set(_tokenize(expected))
    certainty_words = ErrorClassifier._CERTAINTY_MARKERS

    extra_words = c_words - e_words
    c_has_certainty = bool(c_words & certainty_words)

    # 候选有多出的细节 + 确定性表述
    if len(extra_words) > 5 and c_has_certainty:
        return {
            "error_class": ErrorClass.HALLUCINATION,
            "confidence": 0.65,
            "reason": f"候选含 {len(extra_words)} 个期望中不存在的词，且有确定性标记",
            "example": f"额外词: {', '.join(list(extra_words)[:8])}",
        }

    # 候选明显更长 + 无对应期望内容
    if len(candidate) > len(expected) * 1.8:
        return {
            "error_class": ErrorClass.HALLUCINATION,
            "confidence": 0.55,
            "reason": f"候选({len(candidate)}字)远长于期望({len(expected)}字) — 可能编造内容",
            "example": f"候选: {_trunc(candidate, 100)}",
        }

    return None


def _detect_omission(candidate: str, expected: str) -> dict | None:
    """遗漏：期望中的关键内容在候选中缺失."""
    c_lower = candidate.lower()
    e_lower = expected.lower()

    # 分句检测遗漏
    e_sentences = re.split(r'[。！？\n]', expected)
    e_sentences = [s.strip() for s in e_sentences if len(s.strip()) > 5]

    if not e_sentences:
        return None

    missed = []
    for s in e_sentences:
        # 检查关键子串是否在候选中
        keywords = [w for w in _tokenize(s) if len(w) >= 2]
        if keywords:
            hit_count = sum(1 for kw in keywords if kw.lower() in c_lower)
            if hit_count < len(keywords) * 0.3:
                missed.append(s)

    if missed:
        return {
            "error_class": ErrorClass.OMISSION,
            "confidence": min(0.9, 0.5 + len(missed) * 0.1),
            "reason": f"候选遗漏了 {len(missed)}/{len(e_sentences)} 个关键句子",
            "example": f"遗漏: {_trunc(missed[0], 80)}",
        }

    return None


def _detect_precision_issue(candidate: str, expected: str) -> dict | None:
    """精确度：答案方向对但不够具体."""
    if len(candidate) < len(expected) * 0.5:
        return {
            "error_class": ErrorClass.PRECISION,
            "confidence": 0.5,
            "reason": f"候选({len(candidate)}字)过于简短 — 期望({len(expected)}字)",
            "example": f"候选: {_trunc(candidate, 80)}\n期望: {_trunc(expected, 80)}",
        }
    return None


def _detect_format_error(candidate: str, expected: str) -> dict | None:
    """格式不匹配：列表 vs 段落、代码块 vs 纯文本."""
    c_has_list = bool(re.search(r'^[\s]*[-*•\d]+[\.\)]', candidate, re.MULTILINE))
    e_has_list = bool(re.search(r'^[\s]*[-*•\d]+[\.\)]', expected, re.MULTILINE))
    c_has_code = bool(re.search(r'```', candidate))
    e_has_code = bool(re.search(r'```', expected))

    issues = []
    if c_has_list != e_has_list:
        issues.append("列表/段落格式不匹配")
    if c_has_code != e_has_code:
        issues.append("代码块格式不匹配")

    if issues:
        return {
            "error_class": ErrorClass.FORMAT_ERROR,
            "confidence": 0.7,
            "reason": "; ".join(issues),
            "example": f"候选格式: list={c_has_list} code={c_has_code} | 期望: list={e_has_list} code={e_has_code}",
        }

    return None


# ── helpers ──────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """中文 + 英文分词."""
    # 中文按 2-gram + 英文按空格
    tokens = set()
    # 匹配中文词
    for match in re.finditer(r'[\u4e00-\u9fff]+', text):
        word = match.group()
        tokens.add(word)
        # 双字词
        for i in range(len(word) - 1):
            tokens.add(word[i:i + 2])
    # 英文/数字
    for match in re.finditer(r'[a-zA-Z0-9]+', text):
        tokens.add(match.group().lower())
    return tokens


def _trunc(text: str, max_len: int = 100) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _truncate_diff(candidate: str, expected: str, window: int = 60) -> str:
    """截取两段文本中差异最大的区域."""
    if len(candidate) <= window and len(expected) <= window:
        return f"候选: {candidate} | 期望: {expected}"
    return f"候选({len(candidate)}字): {_trunc(candidate, window)}\n期望({len(expected)}字): {_trunc(expected, window)}"


_FIX_SUGGESTIONS: dict[ErrorClass, str] = {
    ErrorClass.FACT_ERROR: "核对源数据，确认候选输出的事实依据",
    ErrorClass.FORMAT_ERROR: "调整输出格式，匹配期望的结构规范",
    ErrorClass.OMISSION: "检查 prompt 是否遗漏了关键指令，补充缺失信息",
    ErrorClass.HALLUCINATION: "降低 temperature，增加事实约束 prompt",
    ErrorClass.PRECISION: "增加输出的细节粒度，必要时增加 tool_call 获取更多信息",
    ErrorClass.UNKNOWN: "人工审核答案差异，可能需要调整 prompt 策略",
}


def _suggest_fix(error_class: ErrorClass) -> str:
    return _FIX_SUGGESTIONS.get(error_class, "人工审核")
