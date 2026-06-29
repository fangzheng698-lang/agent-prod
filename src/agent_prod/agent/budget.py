"""Phase 4.1: BudgetController — token/time 双重预算控制。

每次 Runtime 执行受 token 和时间双重预算约束，超支自动截断并记录。

用法:
    bc = BudgetController(token_limit=100_000, time_limit_ms=60_000)
    for turn in runtime:
        ok, reason = bc.check_and_report(tokens, time_ms)
        if not ok:
            raise BudgetExceeded(reason, bc.report())
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import structlog
    _logger = structlog.get_logger("budget")
    _STRUCTLOG = True
except ImportError:
    _logger = logging.getLogger("budget")
    _STRUCTLOG = False


class BudgetExceeded(Exception):  # noqa: N818
    """预算超支异常。"""

    def __init__(self, reason: str, report: dict[str, Any]):
        self.reason = reason
        self.report = report
        super().__init__(reason)


class BudgetController:
    """token 和时间双重预算控制器。

    线程安全：单线程 Runtime 执行，无需锁。
    """

    def __init__(
        self,
        token_limit: int = 100_000,
        time_limit_ms: int = 60_000,
    ):
        if token_limit < 0:
            raise ValueError(f"token_limit must be >= 0, got {token_limit}")
        if time_limit_ms < 0:
            raise ValueError(f"time_limit_ms must be >= 0, got {time_limit_ms}")

        self.token_limit = token_limit
        self.time_limit_ms = time_limit_ms
        self.tokens_used = 0
        self.time_used_ms = 0

    def check_and_report(self, tokens: int, time_ms: float) -> tuple[bool, str]:
        """检查预算，返回 (ok, reason)。

        累积追踪 token 和时间消耗。超过任一上限返回 False。
        """
        self.tokens_used += tokens
        self.time_used_ms += int(time_ms)

        token_exceeded = self.tokens_used > self.token_limit
        time_exceeded = self.time_used_ms > self.time_limit_ms

        if token_exceeded and time_exceeded:
            reason = (
                f"Both budgets exceeded — "
                f"tokens: {self.tokens_used}/{self.token_limit} "
                f"({self.tokens_used - self.token_limit} over), "
                f"time: {self.time_used_ms}/{self.time_limit_ms}ms "
                f"({self.time_used_ms - self.time_limit_ms}ms over)"
            )
            self._log("budget_exceeded", reason, "both")
            return False, reason

        if token_exceeded:
            over = self.tokens_used - self.token_limit
            reason = (
                f"Token budget exceeded: {self.tokens_used}/{self.token_limit} "
                f"({over} tokens over limit)"
            )
            self._log("budget_exceeded", reason, "token")
            return False, reason

        if time_exceeded:
            over = self.time_used_ms - self.time_limit_ms
            reason = (
                f"Time budget exceeded: {self.time_used_ms}/{self.time_limit_ms}ms "
                f"({over}ms over limit)"
            )
            self._log("budget_exceeded", reason, "time")
            return False, reason

        return True, "ok"

    def raise_if_exceeded(self, tokens: int, time_ms: float) -> None:
        """检查预算，超支时抛出 BudgetExceeded。

        用法：
            bc.raise_if_exceeded(turn_tokens, turn_time_ms)
        """
        ok, reason = self.check_and_report(tokens, time_ms)
        if not ok:
            raise BudgetExceeded(reason, self.report())

    def reset(self) -> None:
        """重置计数器（新执行开始）。"""
        self.tokens_used = 0
        self.time_used_ms = 0

    def report(self) -> dict[str, Any]:
        """生成预算使用报告。"""
        token_pct = round(self.tokens_used / self.token_limit * 100, 2) if self.token_limit else 0
        time_pct = round(self.time_used_ms / self.time_limit_ms * 100, 2) if self.time_limit_ms else 0
        return {
            "token_limit": self.token_limit,
            "time_limit_ms": self.time_limit_ms,
            "tokens_used": self.tokens_used,
            "time_used_ms": self.time_used_ms,
            "token_pct": token_pct,
            "time_pct": time_pct,
            "token_remaining": max(0, self.token_limit - self.tokens_used),
            "time_remaining_ms": max(0, self.time_limit_ms - self.time_used_ms),
        }

    def _log(self, event: str, reason: str, exceeded: str) -> None:
        if _STRUCTLOG:
            _logger.info(event=event, reason=reason, exceeded=exceeded,
                         tokens_used=self.tokens_used, token_limit=self.token_limit,
                         time_used_ms=self.time_used_ms, time_limit_ms=self.time_limit_ms)
        else:
            _logger.info("[%s] %s", event.upper(), reason)
