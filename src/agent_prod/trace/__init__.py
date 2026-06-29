"""agent_prod.trace — 统一 agent trace + adapter 注册表."""

from .adapters import ADAPTER_REGISTRY, AgentTraceAdapter
from .models import (
    AgentTrace,
    AgentType,
    Decision,
    EvaluateResult,
    MetricsSnapshot,
    PolicyTag,
    ToolInvocation,
    TrafficMetrics,
    TrafficStage,
)

__all__ = [
    "AgentTrace", "AgentType", "TrafficStage", "PolicyTag",
    "Decision", "ToolInvocation", "MetricsSnapshot", "TrafficMetrics",
    "EvaluateResult",
    "AgentTraceAdapter", "ADAPTER_REGISTRY",
]
