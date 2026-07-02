# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Anthropic Messages API ↔ OpenAI Chat Completions format conversion.

For the proxy endpoint: Claude Code sends Anthropic-format requests,
the upstream LLM speaks OpenAI protocol, and we convert both ways.
"""

from __future__ import annotations

import json
import uuid


def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tools array to OpenAI tools array.

    Anthropic: {"name": "x", "description": "...", "input_schema": {...}}
    OpenAI:   {"type": "function", "function": {"name": "x", "description": "...", "parameters": {...}}}
    """
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })
    return result


def openai_tools_to_declared(tools: list[dict]) -> list[str]:
    """Extract declared tool names from OpenAI-format tools array."""
    return sorted(set(
        t.get("function", {}).get("name", "")
        for t in tools
        if t.get("function", {}).get("name")
    ))


def anthropic_tool_choice_to_openai(tc: dict | None) -> str | dict:
    """Convert Anthropic tool_choice to OpenAI tool_choice.

    Anthropic: {"type": "auto"} / {"type": "any"} / {"type": "tool", "name": "x"}
    OpenAI:   "auto" / "required" / {"type": "function", "function": {"name": "x"}}
    """
    if not tc:
        return "auto"
    tc_type = tc.get("type", "auto")
    if tc_type == "any":
        return "required"
    if tc_type == "tool":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def anthropic_system_to_openai(system: str | list | None) -> str:
    """Convert Anthropic system prompt (string or block array) to plain string."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [
            block.get("text", "")
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(system)


def anthropic_messages_to_openai(messages: list[dict]) -> list[dict]:
    """Convert Anthropic messages array to OpenAI messages array.

    Handles:
    - Text-only messages (string content)
    - Content block arrays (text, tool_use, tool_result)
    - tool_result → tool role mapping
    """
    openai_msgs = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Simple string content
        if isinstance(content, str):
            openai_msgs.append({"role": role, "content": content})
            continue

        # Content block array
        if isinstance(content, list):
            text_parts = []
            tool_calls = []
            seen_tool_result = False

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    })
                elif btype == "tool_result":
                    seen_tool_result = True
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, list):
                        tr_text = " ".join(
                            b.get("text", "") for b in tr_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        tr_text = str(tr_content)
                    openai_msgs.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": tr_text,
                    })

            if seen_tool_result:
                # tool_result blocks handled above as separate tool-role messages.
                # If there's also a text part in the same user message, include it.
                joined = "\n".join(text_parts).strip()
                if joined:
                    openai_msgs.append({"role": "user", "content": joined})
            elif text_parts and tool_calls:
                openai_msgs.append({
                    "role": "assistant",
                    "content": "\n".join(text_parts),
                    "tool_calls": tool_calls,
                })
            elif tool_calls:
                openai_msgs.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                })
            else:
                openai_msgs.append({
                    "role": role,
                    "content": "\n".join(text_parts),
                })

    return openai_msgs


def anthropic_request_to_openai(body: dict) -> dict:
    """Convert full Anthropic Messages API request to OpenAI Chat Completions."""
    messages = body.get("messages", [])
    tools = body.get("tools")
    system = body.get("system")

    # System prompt → first message
    system_text = anthropic_system_to_openai(system)
    openai_messages = []
    if system_text:
        openai_messages.append({"role": "system", "content": system_text})

    # Convert remaining messages
    openai_messages.extend(anthropic_messages_to_openai(messages))

    result: dict = {
        "model": body.get("model", ""),
        "messages": openai_messages,
        "max_tokens": body.get("max_tokens", 4096),
        "temperature": body.get("temperature", 0.7),
    }

    if tools:
        result["tools"] = anthropic_tools_to_openai(tools)
        result["tool_choice"] = anthropic_tool_choice_to_openai(body.get("tool_choice"))

    stop = body.get("stop_sequences")
    if stop:
        result["stop"] = stop

    return result


def openai_to_anthropic_response(openai_body: dict) -> dict:
    """Convert OpenAI Chat Completions response to Anthropic Messages API."""
    # Generate Anthropic-style IDs
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"

    choices = openai_body.get("choices", [])
    usage = openai_body.get("usage", {})

    if not choices:
        return {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": openai_body.get("model", ""),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    choice = choices[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")

    # Build content blocks
    content: list[dict] = []
    text_content = message.get("content", "")
    if text_content:
        content.append({"type": "text", "text": text_content})

    # Convert tool_calls → tool_use blocks
    for tc in message.get("tool_calls", []):
        tc_id = tc.get("id", "")
        fn = tc.get("function", {})
        try:
            arguments = json.loads(fn.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            arguments = {"_raw": fn.get("arguments", "")}
        content.append({
            "type": "tool_use",
            "id": tc_id,
            "name": fn.get("name", ""),
            "input": arguments,
        })

    # Stop reason mapping
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": openai_body.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }