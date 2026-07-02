# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Agent-level OpenTelemetry integration.

Extends agent-prod's evaluation pipeline with OpenTelemetry spans.
Each gate evaluation is a span under an agent run trace.

Usage:
    from agent_prod.observability.otel import OtelBridge

    bridge = OtelBridge(service_name="agent-prod", endpoint="http://localhost:4317")
    bridge.start_span("agent_prod.evaluate", {"agent": "hermes", "session_id": "ses_001"})
    # ... run gates ...
    bridge.end_span({"status": "production", "gates_passed": 8})

Zero external dependencies — the bridge is a no-op if opentelemetry
is not installed. This allows agent-prod to ship without an OTel hard
dependency; users who want observability pip install opentelemetry-*.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Try to import OTel — gracefully degrade if not available
try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    otel_trace = None  # type: ignore
    OTLPSpanExporter = None  # type: ignore
    Resource = None  # type: ignore
    TracerProvider = None  # type: ignore
    BatchSpanProcessor = None  # type: ignore


# ── Span kind constants (mirror OTel enums without import dep) ──────

SPAN_KIND_INTERNAL = 1
SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3
SPAN_KIND_PRODUCER = 4
SPAN_KIND_CONSUMER = 5


# ── Agent span attributes ──────────────────────────────────────────

AGENT_SPAN_ATTRIBUTES = {
    "agent.id": "",
    "agent.type": "",
    "agent.version": "",
    "agent.session_id": "",
    "agent.run_id": "",
    "gate.name": "",
    "gate.passed": False,
    "gate.score": 0.0,
    "gate.threshold": 0.0,
    "gate.duration_ms": 0.0,
    "pipeline.status": "",
    "pipeline.gates_passed": 0,
    "pipeline.gates_total": 0,
    "pipeline.duration_ms": 0.0,
    "pipeline.fail_gate": "",
    "pipeline.fail_reason": "",
}


class OtelBridge:
    """Bridge between agent-prod and OpenTelemetry.

    Usage:
        bridge = OtelBridge(
            service_name="agent-prod",
            endpoint="http://localhost:4317",
        )

        # Wrap a single gate evaluation
        with bridge.gate_span("gate0_permission", {"agent": "hermes"}):
            result = gate0.verify(improvement)

        # Wrap a pipeline evaluation
        with bridge.pipeline_span("agent_prod.evaluate", {"agent": "hermes"}):
            engine.run_pipeline(improvement)
    """

    def __init__(
        self,
        service_name: str | None = None,
        endpoint: str | None = None,
        enabled: bool = True,
    ):
        self._enabled = enabled and _OTEL_AVAILABLE
        self._tracer = None
        self._current_span = None

        if not self._enabled:
            return

        if not _OTEL_AVAILABLE:
            logger.info(
                "OpenTelemetry not installed — install opentelemetry-* "
                "packages to enable agent observability"
            )
            return

        service_name = service_name or os.environ.get(
            "OTEL_SERVICE_NAME", "agent-prod",
        )
        endpoint = endpoint or os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317",
        )

        try:
            resource = Resource.create({
                "service.name": service_name,
                "service.version": "1.0.0",
            })
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=endpoint)
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            otel_trace.set_tracer_provider(provider)
            self._tracer = otel_trace.get_tracer("agent-prod", "1.0.0")
            logger.info("OTel tracer initialized: %s -> %s", service_name, endpoint)
        except Exception as e:
            logger.warning("OTel initialization failed: %s — observability disabled", e)
            self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled and self._tracer is not None

    # ── Context manager helpers ─────────────────────────────────

    def gate_span(self, gate_name: str, attributes: dict | None = None):
        """Context manager for a single gate evaluation span.

        Usage:
            with bridge.gate_span("gate0_permission", {"agent": "hermes"}):
                result = gate0.verify(improvement)
        """
        if not self.is_enabled:
            return _NoopSpan()
        attrs = dict(AGENT_SPAN_ATTRIBUTES)
        attrs["gate.name"] = gate_name
        if attributes:
            attrs.update(attributes)
        return _SpanContextManager(
            self._tracer.start_as_current_span(
                f"gate.{gate_name}",
                kind=SPAN_KIND_INTERNAL,
                attributes={k: v for k, v in attrs.items() if v is not None and v != ""},
            ),
            self,
        )

    def pipeline_span(self, name: str = "agent_prod.evaluate", attributes: dict | None = None):
        """Context manager for a full pipeline evaluation span.

        Usage:
            with bridge.pipeline_span("agent_prod.evaluate", {"agent": "hermes"}):
                engine.run_pipeline(improvement)
        """
        if not self.is_enabled:
            return _NoopSpan()
        attrs = dict(AGENT_SPAN_ATTRIBUTES)
        if attributes:
            attrs.update(attributes)
        return _SpanContextManager(
            self._tracer.start_as_current_span(
                name,
                kind=SPAN_KIND_SERVER,
                attributes={k: v for k, v in attrs.items() if v is not None and v != ""},
            ),
            self,
        )

    # ── Manual span control (for non-context-manager usage) ─────

    def start_span(self, name: str, attributes: dict | None = None) -> None:
        """Start a new span manually. Must call end_span()."""
        if not self.is_enabled:
            return
        attrs = dict(AGENT_SPAN_ATTRIBUTES)
        if attributes:
            attrs.update(attributes)
        self._current_span = self._tracer.start_span(
            name,
            kind=SPAN_KIND_SERVER,
            attributes={k: v for k, v in attrs.items() if v is not None and v != ""},
        )

    def end_span(self, attributes: dict | None = None) -> None:
        """End the current manual span."""
        if self._current_span is not None:
            if attributes:
                self._current_span.set_attributes({
                    k: v for k, v in attributes.items() if v is not None and v != ""
                })
            self._current_span.end()
            self._current_span = None

    def set_span_attribute(self, key: str, value: Any) -> None:
        """Set an attribute on the current span."""
        if self._current_span is not None and value is not None:
            self._current_span.set_attribute(key, value)


# ── Internal helpers ───────────────────────────────────────────────

class _SpanContextManager:
    """Context manager wrapping an OTel span."""

    def __init__(self, span, bridge: OtelBridge):
        self._span = span
        self._bridge = bridge

    def __enter__(self):
        return self._span.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._span.set_attribute("error", True)
            self._span.set_attribute("error.message", str(exc_val)[:500])
        self._span.__exit__(exc_type, exc_val, exc_tb)


class _NoopSpan:
    """No-op span for when OTel is disabled. Implements the context manager protocol."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key, value):
        pass

    def set_attributes(self, attributes):
        pass

    def end(self):
        pass


# ── Exporter for agent-prod's Span model ───────────────────────────

class AgentSpanExporter:
    """Export agent-prod evaluation results as spans.

    Converts Improvement and GateResult objects into OTel span data
    and exports them via the configured OTel exporter.

    Usage:
        exporter = AgentSpanExporter(endpoint="http://localhost:4317")
        exporter.export_pipeline(improvement)
    """

    def __init__(self, endpoint: str | None = None):
        self._bridge = OtelBridge(endpoint=endpoint)
        self._enabled = self._bridge.is_enabled

    def export_gate(self, gate_name: str, passed: bool, duration_ms: float,
                     score: float = 0.0, threshold: float = 0.0,
                     attributes: dict | None = None) -> None:
        """Export a single gate evaluation as a span."""
        if not self._enabled:
            return
        attrs = {
            "gate.name": gate_name,
            "gate.passed": passed,
            "gate.score": score,
            "gate.threshold": threshold,
            "gate.duration_ms": duration_ms,
        }
        if attributes:
            attrs.update(attributes)
        with self._bridge.gate_span(gate_name, attrs):
            pass

    def export_pipeline(self, improvement, agent_type: str = "") -> None:
        """Export a full pipeline evaluation as a trace with child spans.

        Args:
            improvement: Improvement object with gate_results.
            agent_type: Agent type identifier (e.g. 'hermes', 'claude-code').
        """
        if not self._enabled:
            return

        from agent_prod.gates.models import Improvement

        attrs = {
            "agent.type": agent_type,
            "agent.session_id": improvement.id,
            "pipeline.status": improvement.status.value if hasattr(improvement.status, 'value') else str(improvement.status),
            "pipeline.gates_passed": sum(1 for gr in improvement.gate_results if gr.passed),
            "pipeline.gates_total": len(improvement.gate_results),
            "pipeline.fail_gate": improvement.fail_gate or "",
            "pipeline.fail_reason": (improvement.fail_reason or "")[:200],
        }

        with self._bridge.pipeline_span("agent_prod.evaluate", attrs):
            for gr in improvement.gate_results:
                gate_name = gr.gate_name.value if hasattr(gr.gate_name, 'value') else str(gr.gate_name)
                self.export_gate(
                    gate_name=gate_name,
                    passed=gr.passed,
                    duration_ms=gr.duration_ms,
                    score=gr.details.get("score", 0.0) if isinstance(gr.details, dict) else 0.0,
                    threshold=gr.details.get("threshold", 0.0) if isinstance(gr.details, dict) else 0.0,
                    attributes={"pipeline.id": improvement.id},
                )