"""Heartbeat monitor for proxy-connected agent windows.

Periodically checks all active proxy sessions for heartbeat timeout.
Sessions that miss N consecutive heartbeats are marked as CRASHED,
which triggers the evaluation worker to run the quality gate pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("agent_prod.heartbeat")


class HeartbeatMonitor:
    """Background monitor that detects stale/crashed proxy sessions.

    Runs as an asyncio task alongside the web server.
    """

    def __init__(
        self,
        session_manager,
        check_interval_seconds: float = 15.0,
        stale_timeout_seconds: float = 120.0,
    ):
        self._manager = session_manager
        self._check_interval = check_interval_seconds
        self._stale_timeout = stale_timeout_seconds
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        """Periodically check for stale sessions."""
        while True:
            try:
                await asyncio.sleep(self._check_interval)
                stale = self._manager.detect_stale(self._stale_timeout)
                for session in stale:
                    logger.warning(
                        "Proxy session CRASHED (heartbeat timeout): %s (agent=%s, "
                        "last_seen=%.0fs ago, decisions=%d)",
                        session.session_id,
                        session.agent_type,
                        time.time() - session.last_seen,
                        len(session.decisions),
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat monitor error: %s", e)