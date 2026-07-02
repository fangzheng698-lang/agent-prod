# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Security middleware — API key auth + token-bucket rate limiting.

Zero external dependencies. Pure stdlib + Starlette/FastAPI.

Two standalone middleware functions (not BaseHTTPMiddleware, which has
known issues with streaming and early-response patterns).

Usage:
    from agent_prod.server.security import auth_middleware_factory, rate_limit_middleware_factory

    app.middleware("http")(auth_middleware_factory(api_key="..."))
    app.middleware("http")(rate_limit_middleware_factory())
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# Token-Bucket Rate Limiter
# ═══════════════════════════════════════════


class TokenBucket:
    """Thread-safe token bucket for rate limiting.

    Tokens refill at `rate` per second, capped at `capacity`.
    Each request consumes 1 token. Returns True if allowed.
    """

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False


class RateLimiterState:
    """Shared rate limiter state, usable by the middleware factory."""

    def __init__(self, enabled: bool = True, default_rpm: int = 60, default_burst: int = 10):
        self.enabled = enabled
        self.default_rate = default_rpm / 60.0
        self.default_burst = default_burst
        self.buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def configure_endpoint(self, path: str, rpm: int, burst: int | None = None):
        burst_val = burst if burst is not None else max(1, rpm // 6)
        with self._lock:
            self.buckets[path] = TokenBucket(rate=rpm / 60.0, capacity=burst_val)

    def _get_bucket(self, path: str) -> TokenBucket:
        with self._lock:
            if path not in self.buckets:
                self.buckets[path] = TokenBucket(rate=self.default_rate, capacity=self.default_burst)
            return self.buckets[path]

    def allow(self, path: str) -> bool:
        if not self.enabled:
            return True
        return self._get_bucket(path).consume(1)

    def retry_after(self, path: str) -> int:
        bucket = self._get_bucket(path)
        return max(1, int(1.0 / bucket.rate)) if bucket.rate > 0 else 60

    def get_stats(self) -> dict:
        with self._lock:
            return {
                path: {"tokens": round(b.tokens, 1), "capacity": b.capacity, "rate_per_sec": b.rate}
                for path, b in self.buckets.items()
            }


# Global rate limiter state (initialized once at app startup)
_rate_limiter: RateLimiterState | None = None


# ═══════════════════════════════════════════
# Middleware factories
# ═══════════════════════════════════════════


def auth_middleware_factory(
    api_key: str = "",
    auth_required: bool = False,
) -> Callable:
    """Create an auth middleware function.

    When auth_required=True and api_key is set, all /v1/* endpoints
    require Authorization: Bearer <key> header.
    """

    async def auth_middleware(request: Request, call_next: Callable) -> Response:
        if auth_required and api_key and request.url.path.startswith("/v1/"):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse(
                    status_code=401,
                    content={"error": "missing_authorization", "detail": "Authorization: Bearer <key> required"},
                )
            if auth_header[7:] != api_key:
                return JSONResponse(
                    status_code=403,
                    content={"error": "invalid_api_key", "detail": "API key rejected"},
                )
        return await call_next(request)

    return auth_middleware


def rate_limit_middleware_factory(
    enabled: bool = True,
    default_rpm: int = 60,
    default_burst: int = 10,
) -> Callable:
    """Create a rate limiter middleware function.

    Only applies to /v1/* endpoints. Uses shared global state so stats
    can be queried from health endpoint.
    """
    global _rate_limiter
    _rate_limiter = RateLimiterState(enabled=enabled, default_rpm=default_rpm, default_burst=default_burst)

    async def rate_limit_middleware(request: Request, call_next: Callable) -> Response:
        if not _rate_limiter.enabled:
            return await call_next(request)
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        if not _rate_limiter.allow(request.url.path):
            retry_after = _rate_limiter.retry_after(request.url.path)
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limit_exceeded", "detail": f"Too many requests. Retry after {retry_after}s"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    return rate_limit_middleware


def get_rate_limiter_stats() -> dict:
    """Return rate limiter stats for health endpoint."""
    if _rate_limiter is None:
        return {}
    return _rate_limiter.get_stats()
