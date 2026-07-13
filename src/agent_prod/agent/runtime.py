# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Agent 运行时核心 — Run Loop with streaming support.

Phase 8.1: Added stream_run() for SSE streaming.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from agent_prod.agent.budget import BudgetController, BudgetExceeded
from agent_prod.agent.llm import LLMClient, LLMResponse
from agent_prod.agent.tools import ToolRegistry


class TurnRecord:
    """单轮执行记录。"""

    def __init__(self, idx: int):
        self.index = idx  # 兼容 gateway (t.index)
        self.idx = idx    # 别名
        self.response: LLMResponse | None = None
        self.tool_results: list[dict] = []
        self.tool_calls: list[dict] = []  # Phase 8.1: populated during execution
        self.duration_ms: float = 0
        self.error: str | None = None

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "content": self.response.content if self.response else "",
            "tool_results": self.tool_results,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class AgentRuntime:
    """
    Agent 运行时核心。

    用法:
        runtime = AgentRuntime(llm, tools)
        result = await runtime.run(messages)

    流式:
        async for turn in runtime.stream_run(messages):
            print(turn.response.content)
    """

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        *,
        max_turns: int = 50,
        max_tokens: int = 100_000,
        system_prompt: str | None = None,
        budget: BudgetController | None = None,
        on_turn: Callable[[TurnRecord], Any] | None = None,
        on_error: Callable[[Exception, int], Any] | None = None,
    ):
        self._llm = llm
        self._tools = tools
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._budget = budget
        self.on_turn = on_turn
        self.on_error = on_error

        self.total_cost_estimate: float = 0.0
        self.total_turns: int = 0
        self._turns: list[TurnRecord] = []

    async def run(
        self,
        messages: list[dict],
    ) -> tuple[list[dict], list[TurnRecord]]:
        """非流式执行。返回 (最终消息列表, 每轮记录)。"""
        msgs = list(messages)
        turns: list[TurnRecord] = []
        async for turn, final_msgs in self._run_steps(msgs, turns):
            turns.append(turn)
            msgs = final_msgs
        self._turns = turns
        return msgs, turns

    async def _run_steps(
        self, msgs: list[dict], turn_accumulator: list[TurnRecord] | None = None
    ) -> AsyncGenerator[tuple[TurnRecord, list[dict]], None]:
        """异步产生每一轮的 turn 和最新的 msgs list。

        run() 用它收集成 list；stream_run() 直接 yield turn。
        真流式：每个 turn 完成立刻 yield，消费者立刻看到，无需等所有 turn 完成。
        """
        tools_schema = self._tools.list_schemas()
        prev_turns = turn_accumulator if turn_accumulator is not None else self._turns

        for turn_idx in range(self._max_turns):
            turn = TurnRecord(turn_idx)
            start = time.monotonic()

            # ── 预算检查：基于传进来的 accumulator（不是 self._turns） ──
            total_tokens = sum(
                t.response.tokens_prompt + t.response.tokens_completion
                for t in prev_turns if t.response
            )
            if total_tokens > self._max_tokens:
                turn.error = f"Token budget exceeded ({total_tokens} > {self._max_tokens})"
                yield turn, msgs
                break

            # ── Step 1: LLM 调用 ──
            try:
                resp = await self._llm.chat(msgs, tools_schema)
                turn.response = resp
            except Exception as e:
                turn.error = str(e)
                yield turn, msgs
                if self.on_error:
                    result = self.on_error(e, turn_idx)
                    if hasattr(result, '__await__'):
                        await result
                break

            # ── 追加 assistant 消息 ──
            assistant_msg: dict[str, object] = {"role": "assistant", "content": resp.content or ""}
            if resp.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                    }
                    for tc in resp.tool_calls
                ]
                turn.tool_calls = [
                    {"name": tc.name, "args": tc.arguments}
                    for tc in resp.tool_calls
                ]
            msgs.append(assistant_msg)

            # ── Step 2: 没有 tool call → 结束 ──
            if not resp.tool_calls:
                turn.duration_ms = (time.monotonic() - start) * 1000
                self.total_turns += 1
                if self.on_turn:
                    result = self.on_turn(turn)
                    if hasattr(result, '__await__'):
                        await result
                yield turn, msgs
                break

            # ── Step 3: 执行工具 ──
            for tc in resp.tool_calls:
                result = await self._tools.execute(tc.name, tc.arguments)
                turn.tool_results.append({"name": tc.name, "result_preview": str(result)[:100]})
                msgs.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "name": tc.name, "content": str(result)[:3000],
                })

            turn.duration_ms = (time.monotonic() - start) * 1000
            self.total_turns += 1

            if self.on_turn:
                result = self.on_turn(turn)
                if hasattr(result, '__await__'):
                    await result

            yield turn, msgs

            # Phase 4.1: 预算检查
            if self._budget and turn.response:
                turn_tokens = turn.response.tokens_prompt + turn.response.tokens_completion
                turn_time = turn.duration_ms
                try:
                    self._budget.raise_if_exceeded(turn_tokens, turn_time)
                except BudgetExceeded as e:
                    turn.error = str(e)
                    if turn.response:
                        turn.response.finish_reason = "budget"
                    break

        else:
            if prev_turns:
                prev_turns[-1].error = f"Max turns ({self._max_turns}) reached"

    async def stream_run(
        self, messages: list[dict]
    ) -> AsyncGenerator[TurnRecord, None]:
        """真流式执行 — 每个 turn 完成立刻 yield，不等所有 turn 跑完。

        Phase 8.1: 之前是先 await run() 再 yield list（伪流式），
        现在改为 _run_steps() 直接 yield，消费者边收边显示。
        每个 stream_run 调用维护自己的 turns 累加器，预算基于本次
        实际产生过的 turn 而不是 self._turns（避免跨调用 stale 读）。
        """
        msgs = list(messages)
        local_turns: list[TurnRecord] = []
        async for turn, _final_msgs in self._run_steps(msgs, local_turns):
            local_turns.append(turn)
            yield turn
