"""Phase 4.2: MessageLifecycle — 消息生命周期管理。

消息在 session 中的创建/追加/截断/淘汰由统一的生命周期管理器处理。

用法:
    ml = MessageLifecycle()
    ml.set_system("You are a helpful assistant.")
    ml.add_user("what is 42?")
    ml.add_assistant("The answer is 42")
    ml.add_tool_result("search", "Found answer: 42")

    if ml.estimate_tokens() > 100_000:
        ml.trim_to_budget(100_000)

    messages = ml.to_dict_list()  # or ml.get_messages()
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import structlog
    _logger = structlog.get_logger("lifecycle")
    _STRUCTLOG = True
except ImportError:
    _logger = logging.getLogger("lifecycle")
    _STRUCTLOG = False


# ~4 chars per token (rough approximation, no tokenizer dependency)
CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """字符数估计 token 数（约 4 字符/token）。"""
    return max(1, len(text) // CHARS_PER_TOKEN)


class MessageLifecycle:
    """统一管理会话消息的创建/截断/淘汰。

    线程安全：单 session 使用，无需锁。
    """

    def __init__(self):
        self._messages: list[dict[str, Any]] = []
        self.system_prompt: dict[str, Any] | None = None

    # ── 添加消息 ──

    def set_system(self, content: str) -> None:
        self.system_prompt = {"role": "system", "content": content}

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str, tool_calls: list[dict] | None = None) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._messages.append(msg)

    def add_tool_result(self, tool_name: str, result: str, tool_call_id: str = "") -> None:
        msg: dict[str, Any] = {"role": "tool", "content": result, "name": tool_name}
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        self._messages.append(msg)

    # ── 查询 ──

    def get_messages(self) -> list[dict[str, Any]]:
        """返回完整消息列表（含 system prompt）。"""
        if self.system_prompt:
            return [self.system_prompt] + self._messages
        return list(self._messages)

    def get_final_response(self) -> str:
        """获取最后一条 assistant 消息的 content。"""
        for msg in reversed(self._messages):
            if msg["role"] == "assistant" and msg.get("content"):
                return msg["content"]
        return ""

    def to_dict_list(self) -> list[dict[str, Any]]:
        """转换为可存储的纯 dict 列表。"""
        return self.get_messages()

    # ── Token 估算 ──

    def estimate_tokens(self) -> int:
        """估算当前消息列表的总 token 数（约 4 字符/token）。"""
        total = 0
        messages = self.get_messages()
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += _estimate_tokens(content)
            # tool_calls 也占 token
            for tc in msg.get("tool_calls", []):
                tc_str = str(tc)
                total += _estimate_tokens(tc_str)
        return max(0, total)

    # ── 截断 ──

    def trim_to_budget(self, token_limit: int) -> int:
        """截断消息到 token 预算内。保留 system prompt，从最早的消息开始移除。

        返回移除的消息数量。
        """
        messages = self._messages
        removed = 0

        while self.estimate_tokens() > token_limit and len(messages) > 0:
            old_count = len(messages)
            # 优先删 tool 消息（它们最占空间），然后删最早的消息
            tool_idx = None
            for i, msg in enumerate(messages):
                if msg["role"] == "tool":
                    tool_idx = i
                    break
            if tool_idx is not None:
                messages.pop(tool_idx)
            elif len(messages) > 2:
                # 至少保留最后 1 轮（user + assistant）
                messages.pop(0)
            else:
                break
            if len(messages) < old_count:
                removed += 1

        if removed > 0:
            self._log_trim(removed, self.estimate_tokens(), token_limit)

        return removed

    # ── 工厂 ──

    @classmethod
    def from_dict_list(cls, messages: list[dict[str, Any]]) -> MessageLifecycle:
        """从持久化的消息列表重建。"""
        ml = cls()
        for msg in messages:
            if msg["role"] == "system":
                ml.set_system(msg.get("content", ""))
            elif msg["role"] == "user":
                ml.add_user(msg.get("content", ""))
            elif msg["role"] == "assistant":
                ml.add_assistant(
                    msg.get("content", ""),
                    msg.get("tool_calls", []),
                )
            elif msg["role"] == "tool":
                ml.add_tool_result(
                    msg.get("name", "unknown"),
                    msg.get("content", ""),
                    msg.get("tool_call_id", ""),
                )
        return ml

    def _log_trim(self, removed: int, new_tokens: int, limit: int) -> None:
        if _STRUCTLOG:
            _logger.info(
                event="messages_trimmed",
                removed_count=removed,
                remaining_tokens=new_tokens,
                token_limit=limit,
            )
        else:
            _logger.info(
                "Messages trimmed: removed=%d, tokens=%d/%d",
                removed, new_tokens, limit,
            )
