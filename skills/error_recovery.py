"""Error Recovery：智能错误恢复。

HTTP 层的重试由 httpx/LLMClient 处理。
这里提供调用级别的智能恢复：错误分类、退避、上下文注入。
"""

from __future__ import annotations
import asyncio
import time
from typing import Any, Callable


# ── 简易错误分类 ──

ERROR_KEYWORDS = {
    "rate_limit": (["rate limit", "too many requests", "429", "quota"], 5.0),
    "timeout": (["timeout", "timed out", "deadline"], 2.0),
    "token_limit": (["token limit", "context length", "too many tokens", "maximum context"], 1.0),
    "provider": (["internal server error", "500", "502", "503"], 3.0),
    "bad_request": (["bad request", "400"], 0),
}

DEFAULT_DELAY = 1.0


def classify_error_name(error: Exception) -> str:
    """给错误分个类名"""
    msg = str(error).lower()
    for category, (keywords, _) in ERROR_KEYWORDS.items():
        for kw in keywords:
            if kw in msg:
                return category
    return "unknown"


def get_delay(error: Exception, attempt: int) -> float:
    """根据错误类型计算退避时间"""
    msg = str(error).lower()
    for category, (keywords, base_delay) in ERROR_KEYWORDS.items():
        for kw in keywords:
            if kw in msg:
                return min(base_delay * (2 ** attempt), 30.0)
    return min(1.0 * (2 ** attempt), 30.0)


def is_retryable(error: Exception) -> bool:
    """这个错误值得重试吗"""
    name = classify_error_name(error)
    return name in ("rate_limit", "timeout", "provider")


class ErrorRecovery:
    """
    调用级错误恢复。

    用法:
        recovery = ErrorRecovery()
        result = await recovery.recover(messages, llm_fn)
    """

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self.stats: dict[str, int] = {}

    async def recover(
        self,
        messages: list[dict],
        llm_call: Callable,
    ) -> Any:
        """
        带重试的 LLM 调用。

        参数:
            messages: 当前消息列表（可被修改用于恢复）
            llm_call: 无参的 LLM 调用函数
        """
        last_error = None

        for attempt in range(self.max_retries):
            try:
                result = await llm_call()
                return result
            except Exception as e:
                last_error = e
                cat = classify_error_name(e)
                self.stats[cat] = self.stats.get(cat, 0) + 1

                if not is_retryable(e) or attempt >= self.max_retries - 1:
                    raise

                delay = get_delay(e, attempt)
                messages.append({
                    "role": "system",
                    "content": (
                        f"[Auto Recovery #{attempt + 1}]\n"
                        f"错误: {cat}: {e}\n"
                        f"等待 {delay:.1f}s 后重试。"
                    ),
                })

                if cat == "token_limit":
                    self._truncate(messages)

                await asyncio.sleep(delay)

        raise last_error

    def _truncate(self, messages: list[dict]):
        """Token 超限时截断"""
        if len(messages) <= 3:
            return
        # 保留 system + 最近 user + 最近 assistant
        system = [m for m in messages if m.get("role") == "system"]
        kept = system.copy()

        # 找最后一个 user 和 assistant
        last_user = None
        last_assistant = None
        for m in reversed(messages):
            if m.get("role") == "user" and last_user is None:
                last_user = m
            if m.get("role") == "assistant" and last_assistant is None:
                last_assistant = m
            if last_user and last_assistant:
                break

        if last_user:
            kept.append(last_user)
        if last_assistant:
            kept.append(last_assistant)

        kept.append({
            "role": "system",
            "content": "[Context truncated. Previous turns summarized above.]"
        })

        messages.clear()
        messages.extend(kept)
