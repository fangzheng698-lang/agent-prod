"""
agent-prod — Enterprise-grade AI agent framework.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Layered architecture:

    agent/          Core agent runtime (AgentRuntime, LLMClient, ToolRegistry)
    gates/          Quality gates plug-in system (GatePlugin ABC → 5 built-in gates)
    gateway/        Bridges agent execution to quality gate pipeline
    server/         FastAPI server layer (REST API, config, state)
    observability/  Embedded metrics + execution logging
    adaptivity/     Self-improving layer (adaptive gates, data flywheel, causal attribution)
    testing/        Evaluation infrastructure (benchmark, replay, profiling)
    lifecycle/      Session & memory lifecycle management
"""

__version__ = "0.2.0"
