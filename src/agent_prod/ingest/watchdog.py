"""Hermes Session Watchdog — real-time quality gate injection.

Monitors ~/.hermes/sessions/ for new or updated session files.
When a session file appears or changes, parses it and POSTs the
execution trace to agent-prod's /v1/agent/evaluate endpoint.

Zero dependencies beyond Python stdlib. Polls every second via os.stat().
No modification to Hermes Agent required — completely sidecar.

Usage:
    python -m agent_prod.ingest.watchdog                     # foreground
    python -m agent_prod.ingest.watchdog --daemon            # background
    agent-prod watch                                          # via CLI

Configuration (env vars):
    AGENT_PROD_URL=http://localhost:8000       # agent-prod server
    AGENT_PROD_API_KEY=...                     # optional auth key
    AGENT_PROD_WATCHDOG_INTERVAL=1.0           # poll interval (seconds)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_prod.ingest.parser import parse_session_file
from agent_prod.ingest.feedback import analyze_user_feedback

logger = logging.getLogger("agent_prod.watchdog")

HERMES_SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
DEFAULT_AGENT_PROD_URL = "http://localhost:8000"


class SessionWatchdog:
    """Polling-based filesystem watcher for Hermes session files.

    Tracks session_*.json files in the sessions directory. When a file
    appears or is modified, parses it and submits to quality gate pipeline.
    """

    def __init__(
        self,
        sessions_dir: Path = HERMES_SESSIONS_DIR,
        agent_prod_url: str = DEFAULT_AGENT_PROD_URL,
        api_key: str | None = None,
        poll_interval: float = 1.0,
    ):
        self.sessions_dir = sessions_dir
        self.agent_prod_url = agent_prod_url.rstrip("/")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self._known: dict[str, float] = {}  # filename -> mtime
        self._running = False
        self._submitted_count = 0
        self._error_count = 0

    # ── Public API ────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog loop. Blocks until SIGINT/SIGTERM."""
        self._running = True

        if not self.sessions_dir.exists():
            logger.error(f"Sessions directory not found: {self.sessions_dir}")
            self._maybe_create_dir()

        print(f"👁️  Watchdog started — monitoring {self.sessions_dir}")
        print(f"   Target: {self.agent_prod_url}/v1/agent/evaluate")
        if self.api_key:
            print("   Auth: Bearer token configured")
        print(f"   Poll interval: {self.poll_interval}s")
        print("   Press Ctrl+C to stop\n")

        try:
            while self._running:
                self._poll()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            print(f"\n👁️  Watchdog stopped — {self._submitted_count} submitted, {self._error_count} errors")

    def stop(self) -> None:
        """Signal the watchdog loop to stop."""
        self._running = False

    # ── Internal ──────────────────────────────────────────

    def _maybe_create_dir(self) -> None:
        """Create the sessions directory if it doesn't exist (idempotent)."""
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created sessions directory: {self.sessions_dir}")
        except OSError:
            pass

    def _poll(self) -> None:
        """Scan for new or modified session files."""
        if not self.sessions_dir.exists():
            return

        current_files: set[str] = set()
        for fpath in sorted(self.sessions_dir.glob("session_*.json")):
            fname = fpath.name
            current_files.add(fname)
            try:
                mtime = fpath.stat().st_mtime
            except FileNotFoundError:
                continue

            if fname not in self._known:
                # New file
                self._known[fname] = mtime
                self._process(fpath, reason="new")
            elif mtime > self._known[fname] + 0.1:
                # Modified (0.1s tolerance for NFS/VM timestamp jitter)
                self._known[fname] = mtime
                self._process(fpath, reason="updated")

        # Clean up deleted files from known set
        for fname in list(self._known):
            if fname not in current_files:
                del self._known[fname]

    def _process(self, fpath: Path, reason: str) -> None:
        """Parse a session file and submit to quality gate pipeline."""
        try:
            # Parse
            trace_data = self._parse_session(fpath)
            if trace_data is None:
                return

            # Submit
            result = self._submit_evaluate(trace_data)

            self._submitted_count += 1
            status = result.get("status", "unknown")
            passed = result.get("passed", False)
            icon = "✅" if passed else ("⚠️" if status == "gray" else "❌")
            session_id = trace_data.get("session_id", "?")
            print(f"  {icon} [{reason}] {session_id[:24]} → {status}")

        except Exception as e:
            self._error_count += 1
            logger.warning(f"Failed to process {fpath.name}: {e}", exc_info=True)

    def _parse_session(self, fpath: Path) -> dict | None:
        """Parse a Hermes session file into agent-prod AgentTrace format."""
        import urllib.request as _urllib
        return parse_session_file(fpath, source="hermes_watchdog")

    def _submit_evaluate(self, trace_data: dict) -> dict:
        """POST trace to /v1/agent/evaluate and return quality gate result."""
        url = f"{self.agent_prod_url}/v1/agent/evaluate"
        data = json.dumps(trace_data).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.warning(f"Submit failed: HTTP {e.code} — {body[:200]}")
            return {"status": "error", "error": f"HTTP {e.code}", "passed": False}
        except urllib.error.URLError as e:
            logger.warning(f"Submit failed: {e.reason}")
            return {"status": "error", "error": str(e.reason), "passed": False}


# ═══════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════


def main():
    """Entry point: python -m agent_prod.ingest.watchdog"""
    import argparse

    parser = argparse.ArgumentParser(description="Hermes session watchdog for agent-prod")
    parser.add_argument("--sessions-dir", default=str(HERMES_SESSIONS_DIR),
                        help="Hermes sessions directory")
    parser.add_argument("--url", default=os.environ.get("AGENT_PROD_URL", DEFAULT_AGENT_PROD_URL),
                        help="agent-prod server URL")
    parser.add_argument("--api-key", default=os.environ.get("AGENT_PROD_API_KEY", ""),
                        help="API key for auth")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Poll interval in seconds")

    args = parser.parse_args()

    watchdog = SessionWatchdog(
        sessions_dir=Path(args.sessions_dir),
        agent_prod_url=args.url,
        api_key=args.api_key or None,
        poll_interval=args.interval,
    )

    # Handle SIGTERM gracefully
    signal.signal(signal.SIGTERM, lambda sig, frame: watchdog.stop())
    signal.signal(signal.SIGINT, lambda sig, frame: watchdog.stop())

    watchdog.start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
