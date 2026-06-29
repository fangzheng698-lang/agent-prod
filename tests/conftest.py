"""Shared fixtures for agent-prod tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_prod.gates.models import Improvement
from agent_prod.gates.repository import FileRepository, MemoryRepository


@pytest.fixture
def sample_improvement():
    """Standard Improvement for gate tests."""
    return Improvement(
        name="test-improvement",
        id="imp-test-001",
        candidate_output={
            "final_response": "The answer is 42.",
            "expected_answer": "42",
            "user_question": "What is the answer?",
            "tools_used": ["read_file", "web_search"],
            "token_count": 500,
        },
        llm_calls=[
            {"request_id": "req-1", "response_id": "resp-1", "model": "gpt-4", "duration_ms": 1200},
            {"request_id": "req-2", "response_id": "resp-2", "model": "gpt-4", "duration_ms": 800},
        ],
        tool_calls=[
            {"request_id": "resp-1", "response_id": "read_file-1", "tool": "read_file"},
            {"request_id": "resp-2", "response_id": "web_search-1", "tool": "web_search"},
        ],
        metadata={"agent": "hermes", "source": "test"},
        trace_id="trace-abc-123",
    )


@pytest.fixture
def mock_llm_response():
    """Factory fixture returning a fake OpenAI-style response dict for LLM judge tests."""
    def _make(score=0.85, explanation="Good"):
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({"score": score, "explanation": explanation})
                }
            }]
        }
    return _make


@pytest.fixture
def temp_repository():
    """FileRepository backed by a temp file. Cleans up after test."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name
    repo = FileRepository(tmp_path)
    yield repo
    Path(tmp_path).unlink(missing_ok=True)


@pytest.fixture
def memory_repository():
    """Fresh MemoryRepository for each test."""
    repo = MemoryRepository()
    yield repo
    repo.clear()