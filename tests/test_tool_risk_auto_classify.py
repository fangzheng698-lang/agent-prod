"""Tests for LLM-based auto-classification of unknown tools."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from agent_prod.gates.tool_risk import (
    RiskLevel,
    _AUTO_CLASSIFIED,
    _parse_classifier_response,
    auto_classify_tool,
    clear_auto_classified_cache,
    get_risk,
)


class TestAutoClassifyTool(unittest.TestCase):
    """Tests for auto_classify_tool() and surrounding utilities."""

    def setUp(self):
        clear_auto_classified_cache()

    def tearDown(self):
        clear_auto_classified_cache()

    # ── _parse_classifier_response ─────────────────────────

    def test_parse_direct_json(self):
        """Parse a plain JSON response."""
        content = '{"match": "terminal", "risk": "dangerous", "confidence": 0.95}'
        result = _parse_classifier_response(content)
        self.assertEqual(result["match"], "terminal")
        self.assertEqual(result["risk"], "dangerous")
        self.assertEqual(result["confidence"], 0.95)

    def test_parse_code_block(self):
        """Parse JSON inside a markdown code block."""
        content = '```json\n{"match": "terminal", "risk": "dangerous", "confidence": 0.95}\n```'
        result = _parse_classifier_response(content)
        self.assertEqual(result["match"], "terminal")

    def test_parse_braces(self):
        """Extract JSON from surrounding text by finding braces."""
        content = 'Here is my answer: {"match": "read_file", "risk": "benign", "confidence": 0.8}'
        result = _parse_classifier_response(content)
        self.assertEqual(result["match"], "read_file")

    def test_parse_garbage(self):
        """Non-JSON content returns default safe response."""
        content = "I don't know what this tool does."
        result = _parse_classifier_response(content)
        self.assertIsNone(result["match"])
        self.assertEqual(result["confidence"], 0.0)

    # ── auto_classify_tool — no LLM configured ─────────────

    def test_no_llm_config(self):
        """Without LLM config, auto_classify returns None (no HTTP call)."""
        result = auto_classify_tool("exec", "qclaw", llm_config=None)
        self.assertIsNone(result)

    def test_no_api_key(self):
        """With endpoint but no API key, returns None."""
        result = auto_classify_tool(
            "exec", "qclaw",
            llm_config={"llm_endpoint": "https://api.example.com", "llm_api_key": ""},
        )
        self.assertIsNone(result)

    # ── auto_classify_tool — with mocked LLM ───────────────

    @patch("agent_prod.gates.tool_risk.request.urlopen")
    def test_llm_match_existing_tool(self, mock_urlopen):
        """LLM matches 'exec' -> 'terminal' (dangerous)."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": '{"match": "terminal", "risk": "dangerous", "confidence": 0.95, "reason": "exec runs shell commands"}',
                }
            }]
        }).encode()
        mock_urlopen.return_value = mock_resp

        result = auto_classify_tool(
            "exec", "qclaw",
            llm_config={
                "llm_endpoint": "https://api.example.com/v1",
                "llm_api_key": "test-key",
                "llm_model": "gpt-4o-mini",
            },
        )
        self.assertIsNotNone(result)
        canonical, risk = result
        self.assertEqual(canonical, "terminal")
        self.assertEqual(risk, RiskLevel.DANGEROUS)

        # get_risk should still see exec as unknown (auto-classify is Gate0 concern)
        self.assertIsNone(get_risk("exec", "qclaw"))

    @patch("agent_prod.gates.tool_risk.request.urlopen")
    def test_cache_hit_skips_llm(self, mock_urlopen):
        """Second call with same (agent, tool) uses cache, no HTTP."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": '{"match": "read_file", "risk": "benign", "confidence": 0.9}',
                }
            }]
        }).encode()
        mock_urlopen.return_value = mock_resp

        # First call — should hit LLM
        result1 = auto_classify_tool(
            "read", "qclaw",
            llm_config={
                "llm_endpoint": "https://api.example.com/v1",
                "llm_api_key": "test-key",
            },
        )
        self.assertIsNotNone(result1)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second call — should use cache, not hit LLM
        result2 = auto_classify_tool(
            "read", "qclaw",
            llm_config={
                "llm_endpoint": "https://api.example.com/v1",
                "llm_api_key": "test-key",
            },
        )
        self.assertIsNotNone(result2)
        self.assertEqual(result1, result2)
        self.assertEqual(mock_urlopen.call_count, 1)  # no additional call

    @patch("agent_prod.gates.tool_risk.request.urlopen")
    def test_llm_no_match(self, mock_urlopen):
        """LLM returns null match — returns None, tool stays unknown."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": '{"match": null, "risk": null, "confidence": 0.0, "reason": "no match"}',
                }
            }]
        }).encode()
        mock_urlopen.return_value = mock_resp

        result = auto_classify_tool(
            "obscure_tool_123", "test-agent",
            llm_config={
                "llm_endpoint": "https://api.example.com/v1",
                "llm_api_key": "test-key",
            },
        )
        self.assertIsNone(result)

    @patch("agent_prod.gates.tool_risk.request.urlopen")
    def test_llm_network_failure(self, mock_urlopen):
        """LLM network error returns None gracefully."""
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("Connection refused")

        result = auto_classify_tool(
            "exec", "qclaw",
            llm_config={
                "llm_endpoint": "https://api.example.com/v1",
                "llm_api_key": "test-key",
            },
        )
        self.assertIsNone(result)

    def test_clear_cache(self):
        """clear_auto_classified_cache() empties the dict."""
        _AUTO_CLASSIFIED["test:tool"] = ("terminal", RiskLevel.DANGEROUS)
        self.assertTrue(_AUTO_CLASSIFIED)
        clear_auto_classified_cache()
        self.assertFalse(_AUTO_CLASSIFIED)

    # ── Integration: auto_classify + get_risk ──────────────

    @patch("agent_prod.gates.tool_risk.request.urlopen")
    def test_get_risk_unchanged_after_auto_classify(self, mock_urlopen):
        """auto_classify does NOT modify get_risk() behavior."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": '{"match": "terminal", "risk": "dangerous", "confidence": 0.95}',
                }
            }]
        }).encode()
        mock_urlopen.return_value = mock_resp

        # Before auto-classify, get_risk returns None for unknown tools
        self.assertIsNone(get_risk("exec", "qclaw"))

        # Run auto-classify
        result = auto_classify_tool(
            "exec", "qclaw",
            llm_config={
                "llm_endpoint": "https://api.example.com/v1",
                "llm_api_key": "test-key",
            },
        )
        self.assertIsNotNone(result)

        # get_risk still returns None — auto-classify is a Gate0 layer, not global
        self.assertIsNone(get_risk("exec", "qclaw"))


if __name__ == "__main__":
    unittest.main()