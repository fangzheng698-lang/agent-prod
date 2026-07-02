# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Phase 6.1: Execution Log — 结构化执行日志。

每次 Runtime 执行的完整记录保存到 JSONL 文件。
支持按 session_id 和日期范围查询，以及聚合统计。

用法:
    logger = ExecutionLogger("data/execution_log.jsonl")
    logger.log_execution(record)
    logs = logger.query_log(session_id="sess-1")
    stats = logger.get_stats()
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ExecutionLogRecord(BaseModel):
    """单次执行的完整记录。"""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: str
    session_id: str
    prompt: str = ""
    response: str = ""
    turns: int = 0
    costs: dict = Field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "duration_ms": 0,
    })
    quality_gate_result: dict = Field(default_factory=dict)
    duration_ms: float = 0.0
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        validation_alias="timestamp",
        serialization_alias="created_at",
    )

    tokens_used: float = 0.0
    gate_passed: bool = False


class ExecutionLogger:
    """执行日志记录器，写入 JSONL 文件。

    线程安全：单线程 Runtime 执行，无需锁。
    """

    def __init__(self, file_path: str = "data/execution_log.jsonl"):
        self._file_path = Path(file_path)
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

    def log_execution(self, record: ExecutionLogRecord) -> None:
        """追加一条执行记录到 JSONL 文件。"""
        line = record.model_dump_json()
        with open(self._file_path, "a") as f:
            f.write(line + "\n")

    def query_log(
        self,
        session_id: str | None = None,
        date_range: tuple[str, str] | None = None,
    ) -> list[ExecutionLogRecord]:
        """查询执行日志。

        参数:
            session_id: 按 session_id 过滤
            date_range: (start_iso, end_iso) 按创建时间过滤

        返回:
            匹配的 ExecutionLogRecord 列表
        """
        if not self._file_path.exists():
            return []

        results: list[ExecutionLogRecord] = []
        with open(self._file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = ExecutionLogRecord.model_validate_json(line)
                except Exception:
                    continue

                # 过滤 session_id
                if session_id and record.session_id != session_id:
                    continue

                # 过滤日期范围
                if date_range:
                    start, end = date_range
                    if record.created_at < start or record.created_at > end:
                        continue

                results.append(record)

        return results

    def get_stats(self) -> dict:
        """获取聚合统计。

        返回:
            {
                "total_executions": int,
                "total_sessions": int,
                "total_tokens": {"prompt": int, "completion": int},
                "total_duration_ms": float,
                "avg_turns": float,
                "gate_pass_rate": float,
            }
        """
        if not self._file_path.exists():
            return {
                "total_executions": 0,
                "total_sessions": 0,
                "total_tokens": {"prompt": 0, "completion": 0},
                "total_duration_ms": 0.0,
                "avg_turns": 0.0,
                "gate_pass_rate": 0.0,
            }

        total_executions = 0
        prompt_tokens = 0
        completion_tokens = 0
        total_duration = 0.0
        total_turns = 0
        gate_passed = 0
        sessions: set[str] = set()

        with open(self._file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = ExecutionLogRecord.model_validate_json(line)
                except Exception:
                    continue

                total_executions += 1
                sessions.add(record.session_id)
                prompt_tokens += record.costs.get("prompt_tokens", 0)
                completion_tokens += record.costs.get("completion_tokens", 0)
                total_duration += record.duration_ms
                total_turns += record.turns
                if record.quality_gate_result.get("passed", False):
                    gate_passed += 1

        return {
            "total_executions": total_executions,
            "total_sessions": len(sessions),
            "total_tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
            },
            "total_duration_ms": total_duration,
            "avg_turns": round(total_turns / total_executions, 2) if total_executions else 0.0,
            "gate_pass_rate": round(gate_passed / total_executions, 4) if total_executions else 0.0,
        }


# ── Module-level convenience functions ──

_default_logger: ExecutionLogger | None = None


def _get_default_logger() -> ExecutionLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = ExecutionLogger()
    return _default_logger


def log_execution(record: ExecutionLogRecord) -> None:
    """模块级便捷函数：记录执行日志。"""
    _get_default_logger().log_execution(record)


def query_log(
    session_id: str | None = None,
    date_range: tuple[str, str] | None = None,
) -> list[ExecutionLogRecord]:
    """模块级便捷函数：查询执行日志。"""
    return _get_default_logger().query_log(session_id, date_range)


def get_stats() -> dict:
    """模块级便捷函数：获取聚合统计。"""
    return _get_default_logger().get_stats()
