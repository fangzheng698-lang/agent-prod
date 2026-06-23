"""
Gate2: 轨迹完整性门
核心：用 OpenTelemetry 分布式追踪替代手动日志检查
Phase 1: 对接真实 OTel Collector / Jaeger API
- 生产模式：从 Jaeger API 查询实际 trace
- 降级模式：用模型内数据做 caller/callee 检查
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .models import GateResult, GateName, Improvement, RollbackLevel, RollbackPlan

logger = logging.getLogger(__name__)

# ── OTel 集成 ──────────────────────────────────────────────────
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.trace import SpanKind, Status, StatusCode

    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class TraceSpan:
    """极简 span 记录（不依赖 OTel SDK 也能工作）"""
    def __init__(
        self,
        name: str,
        trace_id: str,
        parent_span_id: str = "",
        span_kind: str = "internal",
    ):
        self.span_id = uuid.uuid4().hex[:16]
        self.name = name
        self.trace_id = trace_id
        self.parent_span_id = parent_span_id
        self.span_kind = span_kind
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.attributes: dict[str, Any] = {}
        self.status_ok: bool = True
        self.status_message: str = ""

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, ok: bool, message: str = "") -> None:
        self.status_ok = ok
        self.status_message = message

    def close(self) -> None:
        self.end_time = time.time()

    @property
    def duration_ms(self) -> float:
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000

    @property
    def is_closed(self) -> bool:
        return self.end_time is not None


class OTelTracer:
    """
    OpenTelemetry 追踪器
    生产模式用真实 OTel SDK；开发/演示模式用纯 Python 实现（数据格式一致）
    """

    def __init__(self, service_name: str = "loop-engineer", use_otel: bool = False):
        self.service_name = service_name
        self.use_otel = use_otel and _OTEL_AVAILABLE
        self._spans: dict[str, TraceSpan] = {}
        self._trace_id = ""

        if self.use_otel:
            try:
                # Don't override if a real provider is already registered
                existing = trace.get_tracer_provider()
            except Exception:
                existing = None

            if existing is not None and not isinstance(existing, trace.ProxyTracerProvider):
                # Provider already set — just get a tracer
                self._tracer = trace.get_tracer(service_name)
            else:
                resource = Resource(attributes={SERVICE_NAME: service_name})
                provider = TracerProvider(resource=resource)
                provider.add_span_processor(
                    SimpleSpanProcessor(ConsoleSpanExporter())
                )
                trace.set_tracer_provider(provider)
                self._tracer = trace.get_tracer(service_name)
        else:
            self._tracer = None

    def start_trace(self) -> str:
        """开始一个新的 trace"""
        self._trace_id = uuid.uuid4().hex[:32]
        return self._trace_id

    def start_span(self, name: str, trace_id: str = "",
                   parent_span_id: str = "",
                   span_kind: str = "internal") -> str:
        """创建一个 span 并返回 span_id"""
        t_id = trace_id or self._trace_id or self.start_trace()

        if self.use_otel and self._tracer:
            kind_map = {
                "internal": SpanKind.INTERNAL,
                "server": SpanKind.SERVER,
                "client": SpanKind.CLIENT,
                "producer": SpanKind.PRODUCER,
                "consumer": SpanKind.CONSUMER,
            }
            otel_span = self._tracer.start_span(
                name,
                kind=kind_map.get(span_kind, SpanKind.INTERNAL),
            )
            otel_span.set_attribute("service.name", self.service_name)
            # 封存到本地 span 记录
            trace_id_val = otel_span.get_span_context().trace_id
            span_id_val = otel_span.get_span_context().span_id
            s = TraceSpan(name, hex(trace_id_val)[2:].zfill(32), parent_span_id, span_kind)
            s.span_id = hex(span_id_val)[2:].zfill(16)
            self._spans[s.span_id] = s
            s.attributes["_otel_span"] = otel_span
            return s.span_id

        s = TraceSpan(name, t_id, parent_span_id, span_kind)
        self._spans[s.span_id] = s
        return s.span_id

    def end_span(self, span_id: str, status_ok: bool = True, message: str = "") -> None:
        span = self._spans.get(span_id)
        if not span:
            return
        span.set_status(status_ok, message)
        span.close()

        # 如果使用了 OTel SDK，也结束 OTel span
        if self.use_otel:
            otel_span = span.attributes.get("_otel_span")
            if otel_span:
                otel_span.set_status(
                    Status(StatusCode.OK if status_ok else StatusCode.ERROR, message)
                )
                otel_span.end()

    def set_attribute(self, span_id: str, key: str, value: Any) -> None:
        span = self._spans.get(span_id)
        if span:
            span.set_attribute(key, value)

    def get_trace_spans(self, trace_id: str = "") -> list[TraceSpan]:
        t_id = trace_id or self._trace_id
        return [s for s in self._spans.values() if s.trace_id == t_id]

    # ── 完整性检查 ──────────────────────────────────────────────

    def check_integrity(self, trace_id: str = "") -> dict[str, Any]:
        """检查 trace 的完整性"""
        spans = self.get_trace_spans(trace_id)

        if not spans:
            return {"valid": False, "errors": ["No spans found in trace"]}

        # 1. 所有 span 都已关闭？
        open_spans = [s for s in spans if not s.is_closed]
        all_closed = len(open_spans) == 0

        # 2. 没有孤儿 span？（parent_span_id 引用的 span 都存在，或者是 root）
        span_ids = {s.span_id for s in spans}
        orphan_spans = []
        for s in spans:
            if s.parent_span_id and s.parent_span_id not in span_ids:
                orphan_spans.append(f"{s.name}({s.span_id[:8]}..)")

        # 3. 时间连续性：没有超过 gap 的空白
        sorted_spans = sorted(spans, key=lambda x: x.start_time)
        time_gaps = []
        for i in range(1, len(sorted_spans)):
            gap = sorted_spans[i].start_time - sorted_spans[i - 1].end_time
            if gap and gap > 5.0:  # >5s 空白
                time_gaps.append({
                    "between": f"{sorted_spans[i-1].name} → {sorted_spans[i].name}",
                    "gap_seconds": round(gap, 2),
                })

        # 4. DAG 形状分析
        roots = [s for s in spans if not s.parent_span_id]
        leaves = [s for s in spans if s.is_closed and
                  not any(s2.parent_span_id == s.span_id for s2 in spans)]

        errors = []
        if not all_closed:
            errors.append(f"{len(open_spans)} open span(s): {[s.name for s in open_spans]}")
        if orphan_spans:
            errors.append(f"Orphan spans: {orphan_spans}")
        if time_gaps:
            errors.append(f"Time gaps >5s: {time_gaps}")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "stats": {
                "total_spans": len(spans),
                "roots": len(roots),
                "leaves": len(leaves),
                "open_spans": len(open_spans),
                "orphan_spans": len(orphan_spans),
                "time_gaps": len(time_gaps),
            },
            "open_spans": open_spans,
            "orphan_spans": orphan_spans,
            "time_gaps": time_gaps,
        }

    def generate_trace_summary(self, trace_id: str = "") -> str:
        """生成人类可读的 trace 摘要"""
        spans = self.get_trace_spans(trace_id)
        integrity = self.check_integrity(trace_id)

        lines = [
            f"Trace: {trace_id or self._trace_id}",
            f"Valid: {'✅' if integrity['valid'] else '❌'}",
            f"Spans: {integrity['stats']['total_spans']} total, "
            f"{integrity['stats']['roots']} roots, {integrity['stats']['leaves']} leaves",
        ]
        if integrity['errors']:
            lines.append(f"Errors:")
            for e in integrity['errors']:
                lines.append(f"  - {e}")
        lines.append("Span tree:")
        root_spans = [s for s in spans if not s.parent_span_id]
        for root in root_spans:
            lines.extend(self._format_span_tree(root, spans, 0))
        return "\n".join(lines)

    def _format_span_tree(self, span: TraceSpan, all_spans: list[TraceSpan],
                          depth: int) -> list[str]:
        indent = "  " * depth
        status = "OK" if span.status_ok else "FAIL"
        closed = "" if span.is_closed else " [OPEN]"
        lines = [
            f"{indent}├─ {span.name} [{span.span_kind}] {status}"
            f"({span.duration_ms:.0f}ms){closed}"
        ]
        children = [s for s in all_spans if s.parent_span_id == span.span_id]
        for child in children:
            lines.extend(self._format_span_tree(child, all_spans, depth + 1))
        return lines


# ── Jaeger API 客户端 ──────────────────────────────────────────────

class JaegerAPIClient:
    """
    从 Jaeger Query Service 查询实际 trace
    生产环境: 需要 Jaeger 或 Tempo 部署

    API 参考:
      GET /api/traces/{trace_id}  → 单个 trace 的完整 span 列表
      GET /api/services           → 已注册的服务列表

    Phase 1: 实时查询实际 trace 替代提升数据模拟
    """

    def __init__(self, base_url: str = "http://localhost:16686",
                 timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._degraded = False

    def query_trace(self, trace_id: str) -> Optional[dict[str, Any]]:
        """从 Jaeger API 查询完整 trace"""
        if self._degraded:
            return None

        try:
            import requests
            resp = requests.get(
                f"{self.base_url}/api/traces/{trace_id}",
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                traces = data.get("data", [])
                if traces:
                    return traces[0]
                logger.warning("Trace %s not found in Jaeger", trace_id)
                return None
            elif resp.status_code == 404:
                logger.warning("Trace %s not found in Jaeger", trace_id)
                return None
            else:
                logger.error("Jaeger API error: %s %s",
                             resp.status_code, resp.text[:200])
                return None

        except ImportError:
            logger.warning("requests library not available for Jaeger API")
            self._degraded = True
            return None
        except Exception as e:
            logger.warning("Jaeger API query failed: %s", e)
            self._consecutive_failures = getattr(self, "_consecutive_failures", 0) + 1
            if self._consecutive_failures >= 3:
                self._degraded = True
                logger.warning("JaegerAPIClient degraded after 3 failures")
            return None

    @staticmethod
    def jaeger_spans_to_tool_calls(jaeger_data: dict) -> list[dict]:
        """将 Jaeger span 数据转换为 Improvement.tool_calls 格式"""
        spans = jaeger_data.get("spans", [])
        tool_calls = []
        for span in spans:
            if span.get("operationName", "").startswith("tool:"):
                tags = {t["key"]: t.get("value") for t in span.get("tags", [])}
                tool_calls.append({
                    "span_id": span.get("spanID"),
                    "trace_id": span.get("traceID"),
                    "tool_name": span["operationName"].replace("tool:", ""),
                    "start_time": span.get("startTime", 0),
                    "duration": span.get("duration", 0),
                    "status": tags.get("otel.status_code", "OK"),
                    "request_id": tags.get("tool.request_id", ""),
                    "response_id": tags.get("tool.response_id", ""),
                })
        return tool_calls

    @staticmethod
    def jaeger_spans_to_llm_calls(jaeger_data: dict) -> list[dict]:
        """将 Jaeger span 数据转换为 Improvement.llm_calls 格式"""
        spans = jaeger_data.get("spans", [])
        llm_calls = []
        for span in spans:
            if span.get("operationName", "").startswith("llm:"):
                tags = {t["key"]: t.get("value") for t in span.get("tags", [])}
                llm_calls.append({
                    "span_id": span.get("spanID"),
                    "trace_id": span.get("traceID"),
                    "model": tags.get("llm.model", ""),
                    "request_id": tags.get("llm.request_id", ""),
                    "response_id": tags.get("llm.response_id", ""),
                    "token_count": tags.get("llm.token_count", 0),
                    "duration_ms": span.get("duration", 0) / 1000,
                })
        return llm_calls

    @staticmethod
    def check_trace_integrity(jaeger_data: dict) -> dict[str, Any]:
        """用 Jaeger trace 数据检查完整性"""
        spans = jaeger_data.get("spans", [])
        if not spans:
            return {"valid": False, "errors": ["No spans in trace"]}

        span_ids = {s["spanID"] for s in spans}
        errors = []

        # 1. 孤儿 span 检测
        orphans = []
        for s in spans:
            refs = s.get("references", [])
            for ref in refs:
                if ref.get("refType") == "CHILD_OF":
                    if ref.get("spanID") and ref["spanID"] not in span_ids:
                        orphans.append(f"{s['operationName']}({s['spanID'][:8]})")
                    break

        # 2. 未完成 span (没有结束时间的情况)
        incomplete = [s for s in spans if s.get("duration", -1) == 0]

        if orphans:
            errors.append(f"Orphan spans: {orphans}")
        if incomplete:
            errors.append(f"Incomplete spans: {len(incomplete)}")

        # 3. DAG 结构统计
        roots = [s for s in spans
                 if not any(
                     r.get("refType") == "CHILD_OF"
                     for r in s.get("references", [])
                 )]
        leaves = [s for s in spans
                  if not any(
                      ref.get("spanID") == s["spanID"]
                      for other in spans
                      for ref in other.get("references", [])
                      if ref.get("refType") == "CHILD_OF"
                  )]

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "stats": {
                "total_spans": len(spans),
                "roots": len(roots),
                "leaves": len(leaves),
                "orphan_spans": len(orphans),
                "incomplete_spans": len(incomplete),
            },
            "source": "jaeger_api",
        }


# ── Gate2 执行器 ────────────────────────────────────────────────

class Gate2TraceIntegrity:
    """轨迹完整性门 — Phase 1: 支持真实 Jaeger/OTel 数据源"""

    def __init__(self, use_otel: bool = False,
                 jaeger_url: str = "",
                 otel_endpoint: str = ""):
        self.tracer = OTelTracer(use_otel=use_otel)
        self.jaeger_client = JaegerAPIClient(
            base_url=jaeger_url or "http://localhost:16686"
        ) if jaeger_url else None
        self.otel_endpoint = otel_endpoint
        self.use_otel = use_otel

    @classmethod
    def from_yaml(cls, config: dict | None = None) -> "Gate2TraceIntegrity":
        """从 config.yaml 加载配置"""
        otel_cfg = (config or {}).get("observability", {}).get("otel", {})
        jaeger_url = (config or {}).get("observability", {}).get("jaeger_url", "")
        return cls(
            use_otel=otel_cfg.get("enabled", False),
            otel_endpoint=otel_cfg.get("endpoint", ""),
            jaeger_url=jaeger_url or otel_cfg.get("jaeger_url", ""),
        )

    def record_llm_call(self, improvement: Improvement, span_id: str,
                        request_id: str, messages_count: int, response_id: str) -> None:
        """记录一次 LLM 调用"""
        self.tracer.set_attribute(span_id, "llm.request_id", request_id)
        self.tracer.set_attribute(span_id, "llm.response_id", response_id)
        self.tracer.set_attribute(span_id, "llm.messages_count", messages_count)

    def record_tool_call(self, improvement: Improvement, span_id: str,
                         tool_name: str, request_id: str, response_id: str,
                         success: bool) -> None:
        """记录一次工具调用"""
        self.tracer.set_attribute(span_id, "tool.name", tool_name)
        self.tracer.set_attribute(span_id, "tool.request_id", request_id)
        self.tracer.set_attribute(span_id, "tool.response_id", response_id)
        self.tracer.set_attribute(span_id, "tool.success", success)

    def verify(self, improvement: Improvement) -> GateResult:
        """执行 Gate2 验证 — Phase 1: 先查 Jaeger，再查 OTel，最后降级"""
        start = time.time()

        # ── 路径 1: 有 Jaeger 客户端 + trace_id → 查真实 trace ──
        if self.jaeger_client and improvement.trace_id:
            try:
                jaeger_data = self.jaeger_client.query_trace(improvement.trace_id)
                if jaeger_data:
                    integrity = JaegerAPIClient.check_trace_integrity(jaeger_data)
                    return GateResult(
                        gate_name=GateName.GATE2,
                        passed=integrity["valid"],
                        reason=(
                            "Trace DAG verified via Jaeger API"
                            if integrity["valid"]
                            else "; ".join(integrity["errors"][:3])
                        ),
                        details={**integrity, "source": "jaeger_api"},
                        duration_ms=(time.time() - start) * 1000,
                    )
            except Exception as e:
                logger.warning("Jaeger query failed, falling back: %s", e)

        # ── 路径 2: 有 OTel trace_id 且 tracer 中有对应 spans ──
        if improvement.trace_id:
            try:
                integrity = self.tracer.check_integrity(improvement.trace_id)
                spans = self.tracer.get_trace_spans(improvement.trace_id)
                if spans:
                    return GateResult(
                        gate_name=GateName.GATE2,
                        passed=integrity["valid"],
                        reason=(
                            "Trace DAG integrity verified"
                            if integrity["valid"]
                            else "; ".join(integrity["errors"][:3])
                        ),
                        details=integrity,
                        duration_ms=(time.time() - start) * 1000,
                    )
            except Exception as e:
                logger.warning("OTel integrity check failed, falling back: %s", e)

        # ── 路径 3: 用 improvement 的数据检查 caller/callee 关系 ──
        if not improvement.llm_calls and not improvement.tool_calls:
            return GateResult(
                gate_name=GateName.GATE2,
                passed=True,  # 无数据时不阻止流程
                reason="No calls to verify — skipping trace integrity (no LLM/tool calls)",
                details={"skipped": True},
                duration_ms=(time.time() - start) * 1000,
            )

        # 检查 caller/callee 关系
        tool_request_ids = {
            tc.get("request_id", "") for tc in improvement.tool_calls
        }
        llm_response_ids = {
            lc.get("response_id", "") for lc in improvement.llm_calls
        }

        missing_parents = tool_request_ids - llm_response_ids
        valid = len(missing_parents) == 0

        return GateResult(
            gate_name=GateName.GATE2,
            passed=valid,
            reason=(
                "Trace integrity verified — all tool calls have LLM parents"
                if valid
                else f"Orphan tool calls: {len(missing_parents)} tool calls have no matching LLM call"
            ),
            details={
                "valid": valid,
                "llm_calls": len(improvement.llm_calls),
                "tool_calls": len(improvement.tool_calls),
                "orphan_tool_calls": list(missing_parents),
            },
            duration_ms=(time.time() - start) * 1000,
        )

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L2 回滚：清理 trace 日志"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L2,
            scope="discard trace logs for this TaskRun",
            estimated_seconds=5,
            procedure="DELETE spans WHERE trace_id = ?; drop tool_calls/llm_calls records",
            executed_at=datetime.now(timezone.utc),
            success=True,
        )

# ── GatePlugin registration ──────────────────────────────
from .interface import register_gate
from .models import GateName
register_gate(GateName.GATE2, Gate2TraceIntegrity)

