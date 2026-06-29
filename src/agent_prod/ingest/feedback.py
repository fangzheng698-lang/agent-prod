"""用户反馈信号检测 — 从对话中自动检测纠错/不满/催促模式。

非硬编码：基于语言学结构模式匹配，而非简单关键词列表。
检测维度：
  1. 直接纠错 (correction): "不对，应该是..." "that's wrong, it should be..."
  2. 重复催促 (repeated_request): 连续多次要求重做
  3. 无奈/不满 (frustration): "还是不行" "又错了" "怎么还"
  4. 语义否定 (semantic_negation): 回答被否定后转向新问题

返回: (satisfaction_score, correction_count, signals_list)
"""

from __future__ import annotations

import re
from typing import Any

# ── 语言学模式（结构级，非单词级） ──────────────────────────

# 模式1: 直接纠错 — 否定词 + 正确表述
CORRECTION_PATTERNS = [
    # 中文: "不对/不是/错了/搞错了" + 纠正内容
    re.compile(r"(不对|不是|错了|搞错了|弄错了|误解了|你理解错了)", re.IGNORECASE),
    # 中文: "应该是..." "实际上是..." "正确的(做法/方式)是..."
    re.compile(r"(应该是|实际上是|正确的|其实|本来)", re.IGNORECASE),
    # 英文: "that's wrong" "no, " "incorrect" "you're missing"
    re.compile(r"\b(wrong|incorrect|not right|no[,]\s|you'?re?\s+(wrong|missing|mistaken))\b", re.IGNORECASE),
    # 英文: "it should be" "actually" "the correct way"
    re.compile(r"\b(it should be|actually|the correct|properly)\b", re.IGNORECASE),
]

# 模式2: 重复催促 — 要求重做/再来
REPEAT_PATTERNS = [
    # 中文: "重新" "再来" "重做" "再试" "redo"
    re.compile(r"(重新|再来|重做|再试|重新来|再做|换个方式)", re.IGNORECASE),
    # 英文: "again" "redo" "retry" "start over" "from scratch"
    re.compile(r"\b(again|redo|retry|start over|from scratch|one more time)\b", re.IGNORECASE),
]

# 模式3: 不满/无奈 — 持续性问题
FRUSTRATION_PATTERNS = [
    # 中文: "还是不行" "又错了" "怎么还" "一直" "总是"
    re.compile(r"(还是不行|还是不对|又错了|怎么还|还是一样|始终|总是|老是|一直)", re.IGNORECASE),
    # 中文: "能不能" 开头的质问
    re.compile(r"^(能不能|可不可以|能否)", re.IGNORECASE),
    # 英文: "still" + failure, "doesn't work", "keeps failing", "broken"
    re.compile(r"\b(still (not|broken|failing|wrong)|doesn'?t work|keeps?\s+(failing|breaking|crashing))\b", re.IGNORECASE),
]

# 模式4: 语义否定 — 回答被否后提出新要求
# 检测: user消息中出现 "不是要...而是要..." "我不要...我要..." 结构
SEMANTIC_NEGATION_PATTERNS = [
    # 中文: "不是要X而是要Y" / "不要X要Y" / "我说的不是X"
    re.compile(r"(不是要|不要|我不是说|我说的不是|你没理解).*(而是|我是说|我是要|我要)", re.IGNORECASE),
    # 英文: "I didn't mean X, I meant Y" / "not X but Y"
    re.compile(r"\b(I\s+didn'?t\s+mean|not\s+\w+\s+but|what\s+I\s+(meant|wanted|asked))\b", re.IGNORECASE),
]


def _score_message(text: str) -> tuple[float, list[str]]:
    """对单条消息进行模式匹配，返回扣分和匹配的模式标签。"""
    score_penalty = 0.0
    signals = []

    # 跨类别叠加，同类只取一次（避免重复匹配叠加）
    has_correction = False
    has_repeat = False
    has_frustration = False
    has_semantic = False

    for i, pat in enumerate(CORRECTION_PATTERNS):
        if pat.search(text):
            has_correction = True
            signals.append(f"correction_{i}")
            break

    for i, pat in enumerate(REPEAT_PATTERNS):
        if pat.search(text):
            has_repeat = True
            signals.append(f"repeat_{i}")
            break

    for i, pat in enumerate(FRUSTRATION_PATTERNS):
        if pat.search(text):
            has_frustration = True
            signals.append(f"frustration_{i}")
            break

    for i, pat in enumerate(SEMANTIC_NEGATION_PATTERNS):
        if pat.search(text):
            has_semantic = True
            signals.append(f"semantic_negation_{i}")
            break

    # 跨类别叠加惩罚（同类只取最大影响）
    if has_correction:
        score_penalty += 0.30  # 直接纠错：较重
    if has_repeat:
        score_penalty += 0.25  # 重复催促
    if has_frustration:
        score_penalty += 0.25  # 不满信号
    if has_semantic and not has_correction:
        score_penalty += 0.20  # 语义否定（不与纠错叠加）

    return score_penalty, signals


def analyze_user_feedback(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """分析对话中的用户反馈信号。

    遍历 user 消息（跳过第一条，那是初始问题），
    检测纠正/不满/催促模式，计算满意度分数。

    Args:
        messages: 会话消息列表，每条含 role + content

    Returns:
        {
            "user_satisfaction": 0.0-1.0,
            "correction_count": int,
            "correction_signals": [str, ...],
            "frustration_detected": bool,
        }
    """
    user_messages = [
        (i, msg) for i, msg in enumerate(messages)
        if msg.get("role") == "user" and msg.get("content")
    ]

    if len(user_messages) <= 1:
        # 只有一条用户消息（初始提问），无反馈可检测
        return {
            "user_satisfaction": 1.0,
            "correction_count": 0,
            "correction_signals": [],
            "frustration_detected": False,
        }

    # 跳过第一条用户消息（初始问题），分析后续的
    total_penalty = 0.0
    all_signals: list[str] = []
    correction_count = 0

    for idx, msg in user_messages[1:]:
        text = str(msg.get("content", ""))
        penalty, signals = _score_message(text)
        if penalty > 0:
            total_penalty += penalty
            all_signals.extend(signals)
            correction_count += 1

    # 连续纠正加重扣分
    if correction_count >= 3:
        total_penalty += 0.15  # 连续纠正 bonus penalty
    if correction_count >= 5:
        total_penalty += 0.25  # 严重信号

    satisfaction = max(0.0, 1.0 - total_penalty)
    frustration = any("frustration" in s for s in all_signals)

    return {
        "user_satisfaction": round(satisfaction, 2),
        "correction_count": correction_count,
        "correction_signals": all_signals,
        "frustration_detected": frustration,
    }
