"""
Alert dispatch — push notifications on quality gate failure.

Pluggable backends: Discord webhook, Telegram bot, generic HTTP webhook.
Configured via config.yaml under the `alerts` section.

Lifecycle:
  - AlertDispatcher is initialized once, holds all configured backends.
  - Engine.run_pipeline() calls dispatcher.send() on gate rejection.
  - Each backend formats and delivers the alert independently.
  - Backends may fail silently (logged, but never block gate evaluation).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Alert Payload ─────────────────────────────────────────────

@dataclass
class AlertPayload:
    """Data carried by a gate-failure alert."""

# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)
    agent_type: str = ""
    agent_version: str = ""
    session_id: str = ""
    improvement_id: str = ""
    improvement_name: str = ""
    failed_gate: str = ""
    fail_reason: str = ""
    gates_summary: list[dict] = field(default_factory=list)
    status: str = "rejected"
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary_text(self) -> str:
        """Human-readable one-line summary."""
        gate = self.failed_gate or "unknown"
        reason = self.fail_reason or "no reason"
        if len(reason) > 120:
            reason = reason[:117] + "..."
        return (
            f"[{self.status.upper()}] {self.agent_type or 'agent'}/{self.session_id or '?'}: "
            f"{gate} — {reason}"
        )

    def to_markdown(self) -> str:
        """Formatted markdown for Discord/Slack."""
        lines = [
            "## 🚨 Quality Gate Alert",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| **Status** | `{self.status.upper()}` |",
            f"| **Agent** | `{self.agent_type or 'unknown'}` |",
            f"| **Session** | `{self.session_id}` |",
            f"| **Improvement** | `{self.improvement_name}` |",
            f"| **Failed Gate** | `{self.failed_gate or '—'}` |",
            f"| **Reason** | {self.fail_reason or '—'} |",
        ]
        if self.gates_summary:
            lines.append("")
            lines.append("### Gate Results")
            for g in self.gates_summary:
                icon = "✅" if g.get("passed") else "❌"
                lines.append(f"- {icon} **{g.get('gate', '?')}** — {g.get('reason', '')[:80]}")
        return "\n".join(lines)


# ── Abstract Backend ──────────────────────────────────────────

class AlertBackend(ABC):
    """One notification channel."""

    @abstractmethod
    def send(self, payload: AlertPayload) -> bool:
        """Deliver alert. Return False on failure (caller logs + swallows)."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging."""
        ...


# ── Discord Backend ───────────────────────────────────────────

class DiscordAlert(AlertBackend):
    """Post to a Discord webhook URL."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    @property
    def name(self) -> str:
        return "discord"

    def send(self, payload: AlertPayload) -> bool:
        try:
            body = json.dumps({
                "content": payload.summary_text(),
                "embeds": [{
                    "title": "Quality Gate Alert",
                    "description": payload.to_markdown(),
                    "color": 0xe74c3c,  # red
                    "timestamp": payload.metadata.get("timestamp", ""),
                }],
            }).encode()
            req = urllib.request.Request(
                self.webhook_url + "?wait=true",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            logger.warning("Discord alert failed: %s", e)
            return False


# ── Telegram Backend ──────────────────────────────────────────

class TelegramAlert(AlertBackend):
    """Send via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def name(self) -> str:
        return "telegram"

    def send(self, payload: AlertPayload) -> bool:
        try:
            text = payload.summary_text()
            url = (
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
                f"?chat_id={self.chat_id}"
                f"&text={urllib.request.quote(text)}"
                f"&parse_mode=Markdown"
            )
            resp = urllib.request.urlopen(url, timeout=10)
            return resp.status == 200
        except Exception as e:
            logger.warning("Telegram alert failed: %s", e)
            return False


# ── Webhook Backend ───────────────────────────────────────────

class WebhookAlert(AlertBackend):
    """POST JSON payload to a generic HTTP endpoint."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = headers or {}

    @property
    def name(self) -> str:
        return "webhook"

    def send(self, payload: AlertPayload) -> bool:
        try:
            body = json.dumps({
                "event": "gate_failure",
                "agent_type": payload.agent_type,
                "session_id": payload.session_id,
                "failed_gate": payload.failed_gate,
                "fail_reason": payload.fail_reason,
                "gates_summary": payload.gates_summary,
                "metadata": payload.metadata,
            }).encode()
            headers = {"Content-Type": "application/json", **self.headers}
            req = urllib.request.Request(self.url, data=body,
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            logger.warning("Webhook alert failed: %s", e)
            return False


# ── Dispatcher (handles multiple backends) ────────────────────

class AlertDispatcher:
    """
    Manages all alert backends and delivers alerts to every
    configured channel on gate failure.
    """

    def __init__(self, backends: list[AlertBackend] | None = None):
        self._backends: list[AlertBackend] = backends or []

    def register(self, backend: AlertBackend) -> None:
        self._backends.append(backend)

    @property
    def backends(self) -> list[AlertBackend]:
        return list(self._backends)

    def send(self, payload: AlertPayload) -> int:
        """Deliver alert to all backends. Returns count of successful deliveries."""
        if not self._backends:
            return 0

        success = 0
        for backend in self._backends:
            try:
                if backend.send(payload):
                    success += 1
                    logger.info("Alert delivered via %s", backend.name)
            except Exception as e:
                logger.warning("Alert backend %s raised: %s", backend.name, e)
        return success


# ── Factory: build from config dict ───────────────────────────

def create_dispatcher_from_config(config: dict | None) -> AlertDispatcher:
    """
    Create an AlertDispatcher from a config dict.

    config.yaml example:
        alerts:
          enabled: true
          discord:
            webhook_url: "https://discord.com/api/webhooks/..."
          telegram:
            bot_token: "123:abc"
            chat_id: "-1001234567890"
          webhook:
            url: "https://hooks.example.com/alerts"
            headers:
              Authorization: "Bearer token123"
    """
    if not config:
        return AlertDispatcher()

    alerts_cfg = config.get("alerts", {})
    if not alerts_cfg.get("enabled", True):
        return AlertDispatcher()

    backends: list[AlertBackend] = []

    # Discord
    discord_cfg = alerts_cfg.get("discord", {})
    if discord_cfg.get("webhook_url"):
        backends.append(DiscordAlert(webhook_url=discord_cfg["webhook_url"]))

    # Telegram
    telegram_cfg = alerts_cfg.get("telegram", {})
    if telegram_cfg.get("bot_token") and telegram_cfg.get("chat_id"):
        backends.append(TelegramAlert(
            bot_token=telegram_cfg["bot_token"],
            chat_id=telegram_cfg["chat_id"],
        ))

    # Generic webhook
    webhook_cfg = alerts_cfg.get("webhook", {})
    if webhook_cfg.get("url"):
        backends.append(WebhookAlert(
            url=webhook_cfg["url"],
            headers=webhook_cfg.get("headers"),
        ))

    return AlertDispatcher(backends)
