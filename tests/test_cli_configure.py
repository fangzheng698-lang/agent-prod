"""Tests for agent-prod configure CLI command."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent_prod.cli_configure import (
    _display_config,
    _generate_default_config,
    _interactive_configure,
    _mask_value,
    cmd_configure,
    CONFIG_PATH,
)


class TestMaskValue:
    def test_empty_string(self):
        assert _mask_value("") == "(empty)"

    def test_short_string(self):
        assert _mask_value("abc") == "****"

    def test_normal_string(self):
        masked = _mask_value("sk-abc123def456")
        assert masked.startswith("sk-a")
        assert masked.endswith("f456")
        assert "abc123" not in masked

    def test_exactly_eight_chars(self):
        assert _mask_value("12345678") == "****"


class TestGenerateDefaultConfig:
    def test_has_all_sections(self):
        config = _generate_default_config()
        assert "gates" in config
        assert "tools" in config
        assert "storage" in config
        assert "observability" in config
        assert "logging" in config
        assert "alerts" in config
        assert "sandbox" in config

    def test_no_hardcoded_api_keys(self):
        config = _generate_default_config()
        # No security section = no API key stored
        assert "security" not in config
        # Also check there's no api_key anywhere in the gate6 section
        gate6 = config.get("gates", {}).get("gate6", {})
        assert "api_key" not in gate6

    def test_no_internal_urls(self):
        config = _generate_default_config()
        gate6 = config["gates"]["gate6"]
        assert gate6["llm_endpoint"] == "https://api.openai.com/v1"

    def test_gate6_default_thresholds(self):
        config = _generate_default_config()
        gate6 = config["gates"]["gate6"]
        assert gate6["pass_threshold"] == 0.58
        assert gate6["evaluator"] == "checklist"

    def test_gate0_has_observe_agents(self):
        config = _generate_default_config()
        per_agent = config["gates"]["gate0"]["per_agent"]
        assert per_agent["claude-code"]["mode"] == "observe"
        assert per_agent["qclaw"]["mode"] == "observe"


class TestDisplayConfig:
    def test_display_does_not_crash(self, capsys):
        config = _generate_default_config()
        _display_config(config)
        captured = capsys.readouterr()
        assert "Gate6 (LLM Evaluation)" in captured.out
        assert "Gate0 (Permission Mode)" in captured.out
        assert "Storage" in captured.out

    def test_display_masks_api_key(self, capsys):
        config = _generate_default_config()
        config["security"] = {"api_key": "sk-test-key-12345"}
        _display_config(config)
        captured = capsys.readouterr()
        assert "sk-t****2345" in captured.out
        assert "sk-test-key-12345" not in captured.out

    def test_display_with_empty_config(self, capsys):
        _display_config({})
        captured = capsys.readouterr()
        assert "Gate6" in captured.out


class TestInteractiveConfigure:
    def test_keeps_config_on_cancel(self):
        config = _generate_default_config()
        with patch("builtins.input", side_effect=KeyboardInterrupt()):
            result = _interactive_configure(config)
            # When cancelled early, the returned config should be the same object
            assert result is config


class TestCmdConfigure:
    def test_show_with_missing_config(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Point CONFIG_PATH to a nonexistent file in tmpdir
            fake_path = Path(tmpdir) / "nonexistent.yaml"
            with patch.object(type(CONFIG_PATH), "exists", return_value=False):
                with pytest.raises(SystemExit):
                    cmd_configure(type("args", (), {"show": True, "reset": False, "mode": None, "agent": None})())

    def test_reset_with_confirmation(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(yaml.dump({"gates": {"mode": "test"}}))
            with (
                patch("agent_prod.cli_configure.CONFIG_PATH", config_path),
                patch("agent_prod.cli_common.CONFIG_PATH", config_path),
                patch("builtins.input", return_value="y"),
            ):
                cmd_configure(type("args", (), {"show": False, "reset": True, "mode": None, "agent": None})())
            written = yaml.safe_load(config_path.read_text())
            assert written["gates"]["gate6"]["llm_endpoint"] == "https://api.openai.com/v1"

    def test_reset_without_confirmation(self, capsys):
        with patch("builtins.input", return_value="n"):
            cmd_configure(type("args", (), {"show": False, "reset": True, "mode": None, "agent": None})())
        captured = capsys.readouterr()
        assert "cancelled" in captured.out.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])