# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""统一错误码枚举 — agent-prod API 所有错误类型。"""

from enum import Enum


class ErrorCode(str, Enum):
    """agent-prod API 错误码，所有 HTTP 响应中携带 code 字段。

    使用: raise AppError(ErrorCode.GATE0_ARG_BLOCKED, reason="...")
    """

    # ── Gate0 权限门 ──
    GATE0_ARG_BLOCKED = "GATE0_ARG_BLOCKED"              # 参数检测到危险内容
    GATE0_TOOL_ELEVATED = "GATE0_TOOL_ELEVATED"          # 工具需要额外授权
    GATE0_TOOL_BLOCKED = "GATE0_TOOL_BLOCKED"            # 工具被黑名单拦截

    # ── Gate1 执行门 ──
    GATE1_SCHEMA_VIOLATION = "GATE1_SCHEMA_VIOLATION"    # 输出格式不匹配契约
    GATE1_BUDGET_EXCEEDED = "GATE1_BUDGET_EXCEEDED"      # Token/时间超预算
    GATE1_CIRCUIT_OPEN = "GATE1_CIRCUIT_OPEN"            # 熔断器开启

    # ── Gate2 轨迹门 ──
    GATE2_TRACE_INCOMPLETE = "GATE2_TRACE_INCOMPLETE"    # 调用链不完整
    GATE2_PARENT_NOT_FOUND = "GATE2_PARENT_NOT_FOUND"    # 工具调用的 LLM parent 不存在

    # ── Gate3 回归门 ──
    GATE3_REGRESSION = "GATE3_REGRESSION"                # 输出回归
    GATE3_PERF_DEGRADED = "GATE3_PERF_DEGRADED"          # 性能下降
    GATE3_NEW_FAILURE_MODE = "GATE3_NEW_FAILURE_MODE"    # 新增失败模式

    # ── Gate4 灰度门 ──
    GATE4_ERROR_RATE_SPIKE = "GATE4_ERROR_RATE_SPIKE"    # 灰度期间错误率飙升
    GATE4_LATENCY_SPIKE = "GATE4_LATENCY_SPIKE"          # 灰度期间延迟飙升
    GATE4_ROLLBACK = "GATE4_ROLLBACK"                    # 灰度回滚

    # ── Gate5 审计门 ──
    GATE5_POLICY_VIOLATION = "GATE5_POLICY_VIOLATION"    # 策略违规
    GATE5_UNAPPROVED = "GATE5_UNAPPROVED"                # 未经审批

    # ── Gate6 答案质量门 ──
    GATE6_QUALITY_BELOW_THRESHOLD = "GATE6_QUALITY_BELOW_THRESHOLD"  # 答案质量低于阈值
    GATE6_EVALUATOR_FAILED = "GATE6_EVALUATOR_FAILED"    # 评估器执行失败

    # ── Pipeline 级 ──
    PIPELINE_TIMEOUT = "PIPELINE_TIMEOUT"                # Pipeline 总体超时
    PIPELINE_INTERNAL_ERROR = "PIPELINE_INTERNAL_ERROR"  # 内部错误

    # ── API 级 ──
    INVALID_TRACE = "INVALID_TRACE"                      # 无效的 trace 输入
    GATEWAY_UNAVAILABLE = "GATEWAY_UNAVAILABLE"          # 网关未初始化
    DB_UNAVAILABLE = "DB_UNAVAILABLE"                    # 数据库不可用
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"                    # 工具不存在


class AppError(Exception):
    """agent-prod 统一异常类型，携带错误码。

    用法:
        raise AppError(ErrorCode.GATE0_ARG_BLOCKED,
                       reason="read_file(/etc/passwd) is blocked",
                       http_status=403)
    """

    def __init__(self, code: ErrorCode, reason: str = "",
                 http_status: int = 400, details: dict | None = None):
        self.code = code
        self.reason = reason
        self.http_status = http_status
        self.details = details or {}
        super().__init__(reason or code.value)

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code.value,
                "reason": self.reason,
                **(self.details),
            }
        }
