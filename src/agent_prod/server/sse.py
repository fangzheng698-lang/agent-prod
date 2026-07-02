# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Phase 8.1: SSE Streaming — Server-Sent Events 流式响应。

FastAPI SSE endpoint: GET /v1/chat/stream，每轮实时推送。

用法:
    from agent_prod.server.sse import stream_response
    @app.get("/v1/chat/stream")
    async def stream_endpoint(prompt: str, session_id: str = ""):
        async for event in stream_response(prompt, session_id, llm, tools):
            yield event
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

from agent_prod.agent.budget import BudgetController
from agent_prod.agent.llm import LLMClient
from agent_prod.agent.runtime import AgentRuntime
from agent_prod.agent.tools import ToolRegistry


async def stream_response(
    prompt: str,
    session_id: str,
    llm: LLMClient,
    tools: ToolRegistry,
    *,
    max_turns: int = 50,
    max_tokens: int = 100_000,
    system_prompt: str | None = None,
) -> AsyncGenerator[str, None]:
    """生成 SSE 事件流。

    Yields SSE格式字符串:
        event: turn_start
        data: {"turn": 1}

        event: response
        data: {"content": "Hello", "finish_reason": "stop"}

        event: tool_call
        data: {"name": "search", "args": {...}, "result": "..."}

        event: turn_end
        data: {"turn": 1, "duration_ms": 1234, "tokens": 56}

        event: done
        data: {"status": "completed", "total_turns": 3, "total_tokens": 150}
    """
    run_id = f"stream_{uuid.uuid4().hex[:8]}"
    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    budget = BudgetController(token_limit=max_tokens, time_limit_ms=60_000)
    runtime = AgentRuntime(
        llm=llm, tools=tools,
        max_turns=max_turns, max_tokens=max_tokens,
        budget=budget,
        system_prompt=system_prompt,
    )

    total_tokens = 0
    turn_idx = 0

    try:
        async for turn in runtime.stream_run(messages):
            turn_idx += 1
            yield _sse("turn_start", {"turn": turn_idx})

            if turn.response:
                total_tokens += turn.response.tokens_prompt + turn.response.tokens_completion
                content = turn.response.content or ""
                # 按行切片推送
                for line in content.split("\n"):
                    yield _sse("response", {
                        "content": line + "\n",
                        "finish_reason": turn.response.finish_reason,
                    })

            if turn.tool_calls:
                for tc in turn.tool_calls:
                    yield _sse("tool_call", {
                        "name": tc.get("name", "unknown"),
                        "args": tc.get("args", {}),
                        "result": tc.get("result", ""),
                    })

            yield _sse("turn_end", {
                "turn": turn_idx,
                "duration_ms": turn.duration_ms,
                "tokens": turn.response.tokens_prompt + turn.response.tokens_completion if turn.response else 0,
            })

            if turn.response and turn.response.finish_reason in ("stop", "budget"):
                break

        yield _sse("done", {
            "status": "completed",
            "run_id": run_id,
            "total_turns": turn_idx,
            "total_tokens": total_tokens,
        })

    except Exception as e:
        yield _sse("error", {"error": str(e), "run_id": run_id})


def _sse(event: str, data: dict) -> str:
    """格式化为 SSE 消息。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
