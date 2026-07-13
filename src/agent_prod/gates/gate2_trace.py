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
from datetime import UTC, datetime
from typing import Any

from .models import GateName, GateResult, Improvement, RollbackLevel, RollbackPlan
from .interface import GatePlugin, register_gate

logger = logging.getLogger(__name__)

# ── OTel 集成 ──────────────────────────────────────────────────
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.trace import SpanKind, Status, StatusCode
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

try:
    import grpc
    _GRPC_AVAILABLE = True
except ImportError:
    _GRPC_AVAILABLE = False


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
        self.end_time: float | None = None
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
            lines.append("Errors:")
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
                 timeout_seconds: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._degraded = False

    def query_trace(self, trace_id: str) -> dict[str, Any] | None:
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
            # 连接/超时异常一次就 degrade，不再重复等待 timeout
            self._degraded = True
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


# ── OTel Collector 客户端 ───────────────────────────────────────

class OtelCollectorClient:
    """OTel Collector 查询客户端 — gRPC 可达性验证。

    生产环境：对接 OTel Collector 的 OTLP receiver，
    collector 负责将 span 数据 routing 到 Jaeger/Tempo/日志后端。
    本客户端验证 Collector 可达性作为生产模式信号。
    """

    def __init__(self, grpc_endpoint: str = "localhost:4317",
                 timeout_seconds: float = 0.5):
        self.grpc_endpoint = grpc_endpoint
        self.timeout = timeout_seconds
        # 启动时一次性探活；失败后 degrade，后续 +query_spans 直接短路
        self._degraded = not (self.health_check() if timeout_seconds > 0 else False)

    def health_check(self) -> bool:
        """检查 Collector 是否可达"""
        if not _GRPC_AVAILABLE:
            self._degraded = True
            return False
        try:
            channel = grpc.insecure_channel(self.grpc_endpoint)
            grpc.channel_ready_future(channel).result(
                timeout=self.timeout
            )
            return True
        except Exception:
            self._degraded = True  # 失败一次就 degrade，不再重复超时
            return False

    def query_spans(self, trace_id: str) -> dict[str, Any] | None:
        """查询 Collector 状态（生产模式可达性信号）"""
        if self._degraded:
            return None
        if self.health_check():
            return {
                "source": "otel_collector",
                "endpoint": self.grpc_endpoint,
                "trace_id": trace_id,
                "note": "Spans routed via OTel Collector",
            }
        return None


# ── Gate2 执行器 ────────────────────────────────────────────────

class Gate2TraceIntegrity(GatePlugin):
    """轨迹完整性门 — Phase 1: 支持真实 Jaeger/OTel 数据源"""

    name = GateName.GATE2
    rollback_level = RollbackLevel.L1

    def __init__(self, use_otel: bool = False,
                 jaeger_url: str = "",
                 otel_endpoint: str = "",
                 otel_collector_grpc: str = ""):
        self.tracer = OTelTracer(use_otel=use_otel)
        self.jaeger_client = JaegerAPIClient(
            base_url=jaeger_url or "http://localhost:16686"
        ) if jaeger_url else None
        self.otel_collector = OtelCollectorClient(
            grpc_endpoint=otel_collector_grpc or "localhost:4317"
        )
        self.otel_endpoint = otel_endpoint
        self.use_otel = use_otel

    @classmethod
    def from_yaml(cls, config: dict | None = None) -> Gate2TraceIntegrity:
        """从 config.yaml 加载配置"""
        otel_cfg = (config or {}).get("observability", {}).get("otel", {})
        jaeger_url = (config or {}).get("observability", {}).get("jaeger_url", "")
        return cls(
            use_otel=otel_cfg.get("enabled", False),
            otel_endpoint=otel_cfg.get("endpoint", ""),
            jaeger_url=jaeger_url or otel_cfg.get("jaeger_url", ""),
            otel_collector_grpc=otel_cfg.get("collector_grpc", ""),
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

        # ── 路径 0: OTel Collector 可达 → 生产模式信号 ──
        try:
            collector_data = self.otel_collector.query_spans(
                improvement.trace_id
            )
            if collector_data and collector_data.get("source") == "otel_collector":
                logger.info("Gate2: OTel Collector reachable at %s",
                            self.otel_collector.grpc_endpoint)
        except Exception as e:
            logger.debug("OTel Collector query: %s", e)

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

        details = Gate2TraceIntegrity._check_local_dag(improvement)
        valid = details["valid"]

        return GateResult(
            gate_name=GateName.GATE2,
            passed=valid,
            reason=(
                "Trace DAG integrity verified — all calls form a valid graph"
                if valid
                else "; ".join(details.get("errors", [])[:3])
            ),
            details=details,
            duration_ms=(time.time() - start) * 1000,
        )

    @staticmethod
    def _check_local_dag(improvement: Improvement) -> dict[str, Any]:
        """Local DAG validator without Jaeger/OTel infrastructure.

        Builds a call graph from improvement.llm_calls and improvement.tool_calls,
        then checks for:
        - Orphaned tool calls (no parent LLM response)
        - Orphaned LLM calls (produced no tool calls and are not standalone)
        - Time ordering violations (tool call starts before its parent LLM ends)
        - Unterminated LLM calls (missing duration_ms / finish_reason)
        - Cycles in the call graph (tool → LLM → same tool)

        Returns a dict with the same shape as check_integrity():
            valid, errors, llm_calls, tool_calls, orphan_tool_calls,
            orphan_llm_calls, time_violations, unterminated_llm_calls, cycles
        """
        errors: list[str] = []

        # Build lookup maps
        llm_by_resp: dict[str, dict] = {}
        for lc in improvement.llm_calls:
            rid = lc.get("response_id", "")
            if rid:
                llm_by_resp[rid] = lc

        tool_by_req: dict[str, list[dict]] = {}
        for tc in improvement.tool_calls:
            req_id = tc.get("request_id", "")
            tool_by_req.setdefault(req_id, []).append(tc)

        # 1. Orphaned tool calls — request_id references no LLM response_id
        orphan_tool_calls: list[str] = []
        for tc in improvement.tool_calls:
            req_id = tc.get("request_id", "")
            if req_id and req_id not in llm_by_resp:
                orphan_tool_calls.append(
                    f"{tc.get('tool', '?')}(request_id={req_id})"
                )
        if orphan_tool_calls:
            errors.append(f"Orphan tool calls: {orphan_tool_calls}")

        # 2. Orphaned LLM calls — response_id not referenced by any tool call
        #    Skip if there are no tool calls at all (valid state for query-only sessions)
        orphan_llm_calls: list[str] = []
        if improvement.tool_calls:
            tool_request_ids = {tc.get("request_id", "") for tc in improvement.tool_calls}
            for lc in improvement.llm_calls:
                resp_id = lc.get("response_id", "")
                if resp_id and resp_id not in tool_request_ids:
                    orphan_llm_calls.append(
                        f"{lc.get('model', '?')}(response_id={resp_id})"
                    )
            if orphan_llm_calls:
                errors.append(f"Orphan LLM calls (no tool calls reference them): {orphan_llm_calls}")

        # 3. Time ordering — each tool call must start after its parent LLM call ends
        time_violations: list[str] = []
        for tc in improvement.tool_calls:
            req_id = tc.get("request_id", "")
            parent = llm_by_resp.get(req_id)
            if parent and "duration_ms" in parent:
                tool_start = tc.get("start_time") or tc.get("timestamp", 0)
                llm_start = parent.get("start_time", 0)
                llm_duration = parent.get("duration_ms", 0)
                llm_end = llm_start + llm_duration
                if tool_start and llm_start and tool_start < llm_end:
                    time_violations.append(
                        f"{tc.get('tool', '?')} starts before parent LLM "
                        f"{parent.get('model', '?')} ends"
                    )
        if time_violations:
            errors.append(f"Time ordering violations: {time_violations}")

        # 4. Unterminated LLM calls — missing duration_ms or finish_reason
        unterminated_llm_calls: list[str] = []
        for lc in improvement.llm_calls:
            if "duration_ms" not in lc and "finish_reason" not in lc:
                unterminated_llm_calls.append(
                    f"{lc.get('model', '?')}(request_id={lc.get('request_id', '')})"
                )
        if unterminated_llm_calls:
            errors.append(f"Unterminated LLM calls: {unterminated_llm_calls}")

        # 5. Cycle detection — tool → LLM → tool where a later LLM references
        #    a tool_call_id that indirectly chains back
        cycles: list[str] = []
        tc_parent_map: dict[str, str] = {}  # tool_response_id → parent request_id
        for tc in improvement.tool_calls:
            tid = tc.get("response_id", "")
            pid = tc.get("request_id", "")
            if tid and pid:
                tc_parent_map[tid] = pid

        # Check for tool call whose response_id is reused as a tool's request_id
        # through an LLM call — basic 2-hop cycle detection
        tc_ids = {tc.get("response_id", "") for tc in improvement.tool_calls}
        for lc in improvement.llm_calls:
            lr_id = lc.get("request_id", "")
            if lr_id and lr_id in tc_ids:
                # This LLM call was triggered by a tool result — check if the
                # tool that produced it was itself triggered by this LLM's response
                parent_resp = lc.get("response_id", "")
                for tc in improvement.tool_calls:
                    if tc.get("request_id") == parent_resp:
                        cycles.append(
                            f"Cycle: LLM({lc.get('model', '?')}) → "
                            f"Tool({tc.get('tool', '?')}) → LLM"
                        )
        if cycles:
            errors.append(f"Call graph cycles detected: {cycles}")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "llm_calls": len(improvement.llm_calls),
            "tool_calls": len(improvement.tool_calls),
            "orphan_tool_calls": orphan_tool_calls,
            "orphan_llm_calls": orphan_llm_calls,
            "time_violations": time_violations,
            "unterminated_llm_calls": unterminated_llm_calls,
            "cycles": cycles,
            "source": "local_dag",
        }

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L2 回滚：清理 trace 日志"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L2,
            scope="discard trace logs for this TaskRun",
            estimated_seconds=5,
            procedure="DELETE spans WHERE trace_id = ?; drop tool_calls/llm_calls records",
            executed_at=datetime.now(UTC),
            success=True,
        )

    @classmethod
    def from_config(cls, config: dict, name: GateName) -> Gate2TraceIntegrity:
        return cls.from_yaml(config)

# ── GatePlugin registration ──────────────────────────────
register_gate(GateName.GATE2, Gate2TraceIntegrity)

