# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Open MCP Registry — discoverable registry for MCP servers.

Usage:
    # Publish a MCP server
    mcp registry publish my-server \\
        --command "uvx my-server" \\
        --description "My MCP server does X" \\
        --tags "search,web"

    # Search for servers
    mcp registry search search

    # Install from registry
    mcp registry install search-web
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("mcp-registry")

# Default registry endpoint — can be overridden via env var
DEFAULT_REGISTRY_URL = "https://mcp-registry.fly.dev"
REGISTRY_URL_ENV = "MCP_REGISTRY_URL"


@dataclass
class MCPEntry:
    """A single MCP server entry in the registry."""

    name: str
    command: str                  # e.g. "uvx my-server"
    description: str
    tags: list[str] = field(default_factory=list)
    version: str = "0.1.0"
    author: str = ""
    homepage: str = ""
    repository: str = ""
    license: str = "MIT"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    source: str = "manual"        # manual | github | npm | pypi

    # Internal
    id: str = ""
    created_at: str = ""
    downloads: int = 0
    verified: bool = False

    def to_json(self) -> dict:
        return {
            "id": self.id or f"mcp-{uuid.uuid4().hex[:8]}",
            "name": self.name,
            "command": self.command,
            "description": self.description,
            "tags": self.tags,
            "version": self.version,
            "author": self.author,
            "homepage": self.homepage,
            "repository": self.repository,
            "license": self.license,
            "args": self.args,
            "env": self.env,
            "source": self.source,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
            "downloads": self.downloads,
            "verified": self.verified,
        }


class RegistryClient:
    """Client for interacting with the MCP registry."""

    def __init__(self, registry_url: str | None = None):
        self.base_url = registry_url or os.environ.get(
            REGISTRY_URL_ENV, DEFAULT_REGISTRY_URL,
        )

    def publish(self, entry: MCPEntry) -> dict:
        """Publish a MCP server to the registry."""
        import urllib.request

        payload = entry.to_json()
        data = json.dumps(payload).encode()
        url = f"{self.base_url.rstrip('/')}/api/v1/servers"
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Registry error {e.code}: {body}") from e

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search for MCP servers in the registry."""
        import urllib.request

        import json as _json

        url = f"{self.base_url.rstrip('/')}/api/v1/servers?q={query}&limit={limit}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Registry error {e.code}: {body}") from e

    def get(self, name: str) -> dict | None:
        """Get a single MCP server entry."""
        import urllib.request

        import json as _json

        url = f"{self.base_url.rstrip('/')}/api/v1/servers/{name}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            body = e.read().decode()
            raise RuntimeError(f"Registry error {e.code}: {body}") from e


# ── Local file-based registry (for offline/first-class use) ──────────

class LocalRegistry:
    """A local file-based registry for MCP servers.

    Stores entries in a JSON file. Good for prototyping and offline use.
    The same schema as the remote registry, so entries can be synced.
    """

    def __init__(self, file_path: str = ""):
        self.file_path = Path(file_path or os.environ.get(
            "MCP_LOCAL_REGISTRY", "~/.mcp/registry.json",
        )).expanduser()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            self._entries = json.loads(self.file_path.read_text())
        except (json.JSONDecodeError, OSError):
            self._entries = {}

    def _save(self) -> None:
        self.file_path.write_text(
            json.dumps(self._entries, indent=2, default=str),
        )

    def add(self, entry: MCPEntry) -> dict:
        """Add or update an entry."""
        data = entry.to_json()
        self._entries[entry.name] = data
        self._save()
        return data

    def search(self, query: str) -> list[dict]:
        """Simple keyword search over name, description, tags."""
        q = query.lower()
        results = []
        for entry in self._entries.values():
            if q in entry["name"].lower() or q in entry["description"].lower():
                results.append(entry)
                continue
            for tag in entry.get("tags", []):
                if q in tag.lower():
                    results.append(entry)
                    break
        return results

    def get(self, name: str) -> dict | None:
        return self._entries.get(name)

    def list_all(self) -> list[dict]:
        return list(self._entries.values())

    def count(self) -> int:
        return len(self._entries)


# ── CLI helpers ─────────────────────────────────────────────────────

def publish_entry(
    name: str,
    command: str,
    description: str,
    tags: list[str] | None = None,
    author: str = "",
    homepage: str = "",
    repository: str = "",
    remote: bool = False,
) -> dict:
    """Publish a MCP server entry locally and optionally to the remote registry."""
    entry = MCPEntry(
        name=name,
        command=command,
        description=description,
        tags=tags or [],
        author=author,
        homepage=homepage,
        repository=repository,
    )

    # Always save locally
    local = LocalRegistry()
    result = local.add(entry)
    logger.info("Published %s to local registry (%d entries)", name, local.count())

    # Optionally publish to remote
    if remote:
        client = RegistryClient()
        remote_result = client.publish(entry)
        logger.info("Published %s to remote registry", name)
        result["remote"] = remote_result

    return result


def search_entries(query: str, local_only: bool = True) -> list[dict]:
    """Search for MCP servers."""
    local = LocalRegistry()
    results = local.search(query)
    if not local_only:
        client = RegistryClient()
        remote_results = client.search(query)
        # Merge, dedup by name, local first
        seen = {r["name"] for r in results}
        for r in remote_results:
            if r["name"] not in seen:
                results.append(r)
                seen.add(r["name"])
    return results