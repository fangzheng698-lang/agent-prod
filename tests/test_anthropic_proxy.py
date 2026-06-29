"""Tests for Anthropic ↔ OpenAI format conversion functions."""
from __future__ import annotations

import json

from agent_prod.server.anthropic_proxy import (
    anthropic_messages_to_openai,
    anthropic_request_to_openai,
    anthropic_system_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai,
    openai_tools_to_declared,
    openai_to_anthropic_response,
)


class TestAnthropicToolsToOpenAI:
    def test_basic_conversion(self):
        tools = [{"name": "get_time", "description": "Get time", "input_schema": {"type": "object"}}]
        result = anthropic_tools_to_openai(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "get_time"
        assert result[0]["function"]["parameters"] == {"type": "object"}

    def test_empty_list(self):
        assert anthropic_tools_to_openai([]) == []


class TestOpenAIToolsToDeclared:
    def test_extracts_names(self):
        tools = [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "write_file"}},
        ]
        assert openai_tools_to_declared(tools) == ["read_file", "write_file"]

    def test_ignores_missing_names(self):
        tools = [{"type": "function", "function": {}}, {"type": "function"}]
        assert openai_tools_to_declared(tools) == []


class TestAnthropicToolChoice:
    def test_auto(self):
        assert anthropic_tool_choice_to_openai({"type": "auto"}) == "auto"

    def test_any_to_required(self):
        assert anthropic_tool_choice_to_openai({"type": "any"}) == "required"

    def test_tool_to_function(self):
        result = anthropic_tool_choice_to_openai({"type": "tool", "name": "calculator"})
        assert result == {"type": "function", "function": {"name": "calculator"}}

    def test_none_returns_auto(self):
        assert anthropic_tool_choice_to_openai(None) == "auto"


class TestAnthropicSystemToOpenAI:
    def test_string_passthrough(self):
        assert anthropic_system_to_openai("You are helpful") == "You are helpful"

    def test_block_array(self):
        system = [{"type": "text", "text": "Be helpful."}, {"type": "text", "text": "Be concise."}]
        assert anthropic_system_to_openai(system) == "Be helpful.\nBe concise."

    def test_none_returns_empty(self):
        assert anthropic_system_to_openai(None) == ""


class TestAnthropicMessagesToOpenAI:
    def test_simple_text_message(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = anthropic_messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello"

    def test_tool_use_conversion(self):
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check"},
                {"type": "tool_use", "id": "tu_1", "name": "calculator", "input": {"expr": "2+3"}},
            ],
        }]
        result = anthropic_messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me check"
        assert len(result[0]["tool_calls"]) == 1
        assert result[0]["tool_calls"][0]["function"]["name"] == "calculator"
        assert json.loads(result[0]["tool_calls"][0]["function"]["arguments"]) == {"expr": "2+3"}

    def test_tool_result_conversion(self):
        msgs = [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "5"},
            ],
        }]
        result = anthropic_messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tu_1"
        assert result[0]["content"] == "5"

    def test_empty_content(self):
        msgs = [{"role": "user", "content": ""}]
        result = anthropic_messages_to_openai(msgs)
        assert result[0]["content"] == ""


class TestAnthropicRequestToOpenAI:
    def test_full_conversion(self):
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "test_tool", "description": "A tool", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "auto"},
            "temperature": 0.5,
        }
        result = anthropic_request_to_openai(body)
        assert result["model"] == "claude-sonnet-4-6"
        assert result["max_tokens"] == 1024
        assert result["temperature"] == 0.5
        assert len(result["messages"]) == 2  # system + user
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][1]["content"] == "hi"
        assert len(result["tools"]) == 1
        assert result["tool_choice"] == "auto"


class TestOpenAIToAnthropicResponse:
    def test_text_response(self):
        openai = {
            "id": "chatcmpl-abc",
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_to_anthropic_response(openai)
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Hello!"
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_tool_calls_response(self):
        openai = {
            "id": "chatcmpl-abc",
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expr":"2+3"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_to_anthropic_response(openai)
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["name"] == "calculator"
        assert result["content"][0]["input"] == {"expr": "2+3"}
        assert result["stop_reason"] == "tool_use"

    def test_no_choices(self):
        openai = {"id": "chatcmpl-abc", "choices": [], "usage": {}}
        result = openai_to_anthropic_response(openai)
        assert result["content"] == []
        assert result["stop_reason"] == "end_turn"

    def test_length_finish_reason(self):
        openai = {
            "id": "chatcmpl-abc",
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "partial"},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 200},
        }
        result = openai_to_anthropic_response(openai)
        assert result["stop_reason"] == "max_tokens"