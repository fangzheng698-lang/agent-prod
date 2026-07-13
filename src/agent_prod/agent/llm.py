# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""OpenAI 协议客户端。纯 httpx，无框架依赖。

所有兼容 OpenAI 的 API（长亭百智云、DeepSeek、vLLM 等）都能用。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    tokens_prompt: int = 0
    tokens_completion: int = 0
    model: str = ""


class LLMClient:
    """OpenAI 协议客户端。线程安全。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.model = model
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """调用 LLM。返回结构化响应。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools

        resp = await self._client.post(
            "/chat/completions", json=body,
            auth=httpx.BearerAuth(self._api_key),
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # httpx HTTPStatusError encodes the request (incl. Authorization
            # header) in str(e). Strip it before re-raising so the API key
            # never appears in tracebacks/logs.
            raise httpx.HTTPStatusError(
                message=f"upstream LLM {resp.status_code}: {self._redact(resp.text)}",
                request=httpx.Request("POST", "REDACTED", headers={}),
                response=resp,
            ) from e
        data = resp.json()

        choice = data["choices"][0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})

        tool_calls = []
        for tc in msg.get("tool_calls", []):
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append(ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=args if isinstance(args, dict) else {},
            ))

        return LLMResponse(
            content=msg.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            tokens_prompt=usage.get("prompt_tokens", 0),
            tokens_completion=usage.get("completion_tokens", 0),
            model=data.get("model", self.model),
        )

    async def close(self):
        await self._client.aclose()

    @staticmethod
    def _redact(body: str, max_len: int = 200) -> str:
        """脱敏可能含 API key 的响应体：只返回前 max_len 字符且用 *** 替换可能的 key。

        上游 LLM 错误响应有时会反射回 request（包括 Authorization header）。
        这里暴力截断 + 替换所有 Bearer/Api-Key 模式。
        """
        import re as _re
        redacted = _re.sub(
            r"Bearer\s+\S+" , "Bearer ***",
            _re.sub(r"sk-[A-Za-z0-9]{20,}", "sk-***", body),
        )
        if len(redacted) > max_len:
            redacted = redacted[:max_len] + "... (truncated)"
        return redacted
