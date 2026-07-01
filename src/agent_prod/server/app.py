"""agent-prod FastAPI application — routes, middleware, and initialization.

Phase 13: Multi-agent /v1/agent/evaluate + /v1/tool/execute (Gate0 runtime).
Phase 12: Unified agent trace evaluation endpoint.
Phase 10: Embedded metrics, embedded quality gates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agent_prod.server.config import settings
from agent_prod.server.schemas import (
    ChatChoice,
    ChatRequest,
    ChatResponse,
    GateResultOut,
    HealthResponse,
    QualityGateResult,
    SessionInfo,
    ToolCallOut,
)

logger = logging.getLogger("agent_prod.server")

# ═══════════════════════════════════════════════════════════════
#  LLM Client
# ═══════════════════════════════════════════════════════════════

from agent_prod.agent.llm import LLMClient

llm = LLMClient(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
    model=settings.openai_model,
) if settings.openai_api_key else None

if llm:
    logger.info(f"Agent API ready: {settings.openai_model} @ {settings.openai_base_url}")
else:
    logger.warning("LLM client DISABLED — no API key configured")

# ═══════════════════════════════════════════════════════════════
#  Tool Registry
# ═══════════════════════════════════════════════════════════════

from agent_prod.agent.tools import ToolRegistry, register_extended_tools

tools = register_extended_tools(ToolRegistry())
logger.info(f"Tools: {list(tools._tools.keys())}")

# ═══════════════════════════════════════════════════════════════
#  Session Store
# ═══════════════════════════════════════════════════════════════

from agent_prod.server.state import StateStore

store = StateStore(settings.database_url)
logger.info(f"Database: {settings.database_url}")

# ═══════════════════════════════════════════════════════════════
#  Quality Gate Engine
# ═══════════════════════════════════════════════════════════════

from agent_prod.gateway.gateway import QualityGateGateway

gateway = None
if settings.quality_gates_enabled:
    if settings.quality_gates_mode == "memory":
        # Memory mode: zero external deps, uses MemoryRepository + default config
        try:
            gateway = QualityGateGateway.memory()
            logger.info("Quality Gates: ENABLED (memory mode)")
        except Exception as e:
            logger.warning("Quality Gates: failed to init memory mode (%s)", e)
    else:
        # Production mode: load from config.yaml (may need Postgres/Jaeger/etc.)
        try:
            config_path = os.environ.get("QUALITY_GATES_CONFIG")
            gateway = QualityGateGateway.from_config(config_path)
            logger.info("Quality Gates: ENABLED (from_config%s)",
                         f" @ {config_path}" if config_path else " (default config.yaml)")
        except Exception as e:
            logger.warning("Quality Gates: DEGRADED (%s)", e)
else:
    logger.info("Quality Gates: DISABLED")

from starlette.requests import Request

# ═══════════════════════════════════════════════════════════════
#  FastAPI Application
# ═══════════════════════════════════════════════════════════════

# Watchdog state for health endpoint
_watchdog_active = False
_watchdog_thread: threading.Thread | None = None


def _start_watchdog_thread(port: int) -> None:
    """Start the Hermes session watchdog as a background thread.

    Zero-touch integration: users don't need to run ``agent-prod watch``
    separately.  The server auto-starts a file-system poller that feeds
    Hermes session files into the quality gate pipeline.
    """
    global _watchdog_active, _watchdog_thread
    try:
        from agent_prod.ingest.watchdog import SessionWatchdog
        from pathlib import Path

        wd = SessionWatchdog(
            sessions_dir=Path.home() / ".hermes" / "sessions",
            agent_prod_url=f"http://localhost:{port}",
            poll_interval=2.0,
        )
        _watchdog_active = True

        def _run():
            try:
                wd.start()
            except Exception:
                pass

        _watchdog_thread = threading.Thread(target=_run, daemon=True,
                                              name="agent-prod-watchdog")
        _watchdog_thread.start()
        logger.info("Watchdog: auto-started (monitoring ~/.hermes/sessions/)")
    except Exception as e:
        logger.warning(f"Watchdog: failed to start ({e})")


app = FastAPI(
    title="agent-prod",
    description="Enterprise Agent Quality Gate Platform — Gate0-Gate7 pipeline with answer quality evaluation",
    version="1.0.0",
)


@app.on_event("startup")
async def _startup_watchdog():
    """Auto-start Hermes session watchdog on server boot.

    Runs in a thread to avoid blocking the event loop.
    No-op if already started via cmd_serve.
    """
    global _watchdog_active, _watchdog_thread
    if _watchdog_active:
        return
    # Read port from uvicorn config or default
    port = int(os.environ.get("AGENT_PROD_PORT", "8000"))
    _start_watchdog_thread(port)

# ── API Versioning Middleware ─────────────────────────────────
# /v1/ 路由已标记为 deprecated，客户端应在 2026-09-01 前迁移到 /v2/
# /v2/ 当前完全兼容，直接映射到相同 handler


@app.middleware("http")
async def add_deprecation_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/v1/"):
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = "Mon, 01 Sep 2026 00:00:00 GMT"
        response.headers["Link"] = '</v2/{}>; rel="successor-version"'.format(
            request.url.path[4:]
        )
    return response


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
from agent_prod.server.security import (
    auth_middleware_factory,
    rate_limit_middleware_factory,
)
if settings.auth_required and settings.api_key:
    app.middleware("http")(auth_middleware_factory(api_key=settings.api_key))
elif settings.auth_required and not settings.api_key:
    logger.warning("auth_required=True but no api_key configured — auth DISABLED. Set AGENT_PROD_API_KEY or api_key in .env")
if settings.rate_limit_enabled:
    app.middleware("http")(rate_limit_middleware_factory())

# Metrics
from agent_prod.observability.metrics import get_registry


# ═══════════════════════════════════════
# API 路由
# ═══════════════════════════════════════

@app.get("/health", response_model=HealthResponse)
async def health():
    """健康检查 — 含存储读写验证。"""
    active = 0
    repo_ok = False
    if store:
        sessions = store.list_sessions(limit=100)
        active = sum(1 for s in sessions if s["status"] == "active")
    # 验证 quality gate repository 是否可用
    if gateway and hasattr(gateway.engine, 'repository') and gateway.engine.repository:
        try:
            gateway.engine.repository.count()
            repo_ok = True
        except Exception:
            repo_ok = False
    return HealthResponse(
        status="ok",
        model=settings.openai_model,
        sessions_active=active,
        quality_gates=gateway is not None and settings.quality_gates_enabled,
        repository=repo_ok,
        auth_enabled=settings.auth_required and bool(settings.api_key),
        rate_limit_enabled=settings.rate_limit_enabled,
        watchdog_active=_watchdog_active,
        gate1_degraded=gateway.engine.gate1_degraded if gateway else False,
    )


@app.get("/ready")
async def ready():
    """Kubernetes readiness probe.

    Returns 200 only when all subsystems are initialized and healthy.
    Used by k8s to determine when the pod can receive traffic.
    """
    checks = {}

    # 1. LLM client. Gate6 can run in checklist/degraded mode without a
    # configured external evaluator, so lack of an API key should not keep the
    # service out of readiness.
    if llm:
        checks["llm"] = "ok"
    else:
        checks["llm"] = "degraded (not configured)"

    # 2. Tools
    if tools:
        checks["tools"] = f"ok ({len(tools._tools)} tools)"
    else:
        checks["tools"] = "not_ready"

    # 3. State store
    if store:
        try:
            store.list_sessions(limit=1)
            checks["store"] = "ok"
        except Exception as e:
            checks["store"] = f"error: {e}"
    else:
        checks["store"] = "not_ready"

    # 4. Quality gate engine
    if gateway:
        try:
            repo_type = type(gateway.engine.repository).__name__
            checks["quality_gates"] = f"ok ({repo_type})"
        except Exception as e:
            checks["quality_gates"] = f"error: {e}"
    else:
        checks["quality_gates"] = "degraded"

    all_ok = all(
        v.startswith("ok") or v.startswith("degraded")
        for v in checks.values()
    )

    status_code = 200 if all_ok else 503
    return JSONResponse(
        content={"ready": all_ok, "checks": checks},
        status_code=status_code,
    )


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint (embedded, zero external deps).

    Scraped directly by Prometheus, or viewed at http://localhost:8000/metrics.
    """
    registry = get_registry()
    return Response(registry.render(), media_type="text/plain; version=0.0.4")


@app.post("/v1/chat/completions", response_model=ChatResponse)
async def chat_completions(req: ChatRequest):
    """
    OpenAI 兼容的聊天补全接口。

    Phase 3: 输入消息 → Agent Runtime 执行 → Quality Gates 验证 → 返回结果。
    门禁不通过的结果在 quality_gate 字段中体现，status=rejected。
    """
    _start = time.monotonic()
    if not llm or not store or not tools:
        raise HTTPException(503, "Service not ready")

    # ── 会话管理 ──
    session_id = req.session_id or f"ses_{uuid.uuid4().hex[:12]}"

    if req.session_id:
        # 续传：从数据库加载历史
        existing = store.get_session(session_id)
        if existing and existing["status"] == "active":
            messages = store.get_messages(session_id)
        else:
            messages = []
        # 追加本轮输入
        messages.extend(req.messages)
    else:
        # 新会话
        messages = list(req.messages)
        store.create_session(session_id, {"system_prompt": req.system_prompt})

    # ── 注入 system prompt ──
    if req.system_prompt and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": req.system_prompt})

    # ── 创建 Runtime 并执行 ──
    from agent_prod.agent.budget import BudgetController
    budget = BudgetController(
        token_limit=settings.max_tokens,
        time_limit_ms=60_000,  # 60s default
    )
    runtime = AgentRuntime(
        llm=llm,
        tools=tools,
        max_turns=settings.max_turns,
        max_tokens=settings.max_tokens,
        budget=budget,
        on_turn=None,
    )

    # ── Phase 4.4: TaskRun 状态机 ──
    from agent_prod.lifecycle.task_state import TaskRun
    task_run = TaskRun(session_id=session_id)
    task_run.mark_running()

    try:
        final_messages, turns = await runtime.run(messages)
    except Exception as e:
        task_run.mark_error(str(e))
        store.update_status(session_id, "error", str(e))
        raise HTTPException(500, f"Agent runtime error: {e}") from e

    task_run.mark_gate_eval()

    # ── Phase 3: Quality Gates 门禁流水线 ─────────────────────
    quality_gate: QualityGateResult | None = None
    gate_all_passed = True

    if gateway and settings.quality_gates_enabled:
        try:
            improvement, gate_all_passed = await gateway.validate(
                session_id, final_messages, turns,
            )
            gate_dict = gateway.gate_results_to_dict(improvement)
            quality_gate = QualityGateResult(
                status=gate_dict["status"],
                passed=gate_dict["passed"],
                gates=[
                    GateResultOut(
                        gate=g["gate"],
                        passed=g["passed"],
                        reason=g["reason"],
                        duration_ms=g["duration_ms"],
                    )
                    for g in gate_dict["gates"]
                ],
                failed_at=gate_dict.get("failed_at"),
                fail_reason=gate_dict.get("fail_reason"),
            )
        except Exception as e:
            # 门禁故障不放行业务
            quality_gate = QualityGateResult(
                status="gate_error",
                passed=False,
                gates=[],
                fail_reason=f"Gate engine error: {e}",
            )
            gate_all_passed = False

    # ── Phase 4.4: 记录门禁结果到状态机 ──
    if gate_all_passed and quality_gate:
        task_run.mark_approved(quality_gate.status)
    elif quality_gate and quality_gate.failed_at:
        task_run.mark_rejected(quality_gate.failed_at, quality_gate.fail_reason or "")

    # ── 持久化 ──
    if gate_all_passed:
        store.save_checkpoint(session_id, final_messages)
        store.update_status(session_id, "completed")
    else:
        store.save_checkpoint(session_id, final_messages)
        store.update_status(
            session_id,
            "rejected",
            quality_gate.fail_reason if quality_gate else "Gate rejected",
        )

    # ── Phase 10: Embedded metrics ──
    duration_ms = (time.monotonic() - _start) * 1000
    registry = get_registry()
    registry.counter("agent_requests_total", "Total agent requests").inc()
    registry.histogram("agent_request_duration_ms", "Request duration").observe(duration_ms)
    if gate_all_passed:
        registry.counter("agent_requests_passed", "Requests passing all gates").inc()
    else:
        registry.counter("agent_requests_rejected", "Requests rejected by gates").inc()
    registry.gauge("agent_sessions_active", "Active sessions").set(
        len([s for s in (store.list_sessions(limit=100) if store else []) if s.get("status") == "active"])
    )

    # ── 构造响应 ──
    last_turn = turns[-1] if turns else None
    final_content = last_turn.response.content if last_turn and last_turn.response else ""
    final_tool_calls = last_turn.response.tool_calls if last_turn and last_turn.response else []

    return ChatResponse(
        id=session_id,
        session_id=session_id,
        choices=[
            ChatChoice(
                content=final_content,
                tool_calls=[
                    ToolCallOut(id=tc.id, name=tc.name, arguments=tc.arguments)
                    for tc in final_tool_calls
                ],
                finish_reason=last_turn.response.finish_reason if last_turn and last_turn.response else "stop",
            )
        ],
        usage={
            "prompt_tokens": sum(t.response.tokens_prompt for t in turns if t.response),
            "completion_tokens": sum(t.response.tokens_completion for t in turns if t.response),
            "total_turns": len(turns),
        },
        quality_gate=quality_gate,
    )


@app.get("/sessions", response_model=list[SessionInfo])
async def list_sessions(limit: int = 20):
    """列出最近的会话"""
    if not store:
        return []
    return store.list_sessions(limit=limit)


@app.get("/sessions/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str):
    """获取某个会话的状态"""
    if not store:
        raise HTTPException(503)
    row = store.get_session(session_id)
    if not row:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return SessionInfo(
        id=row["id"],
        status=row["status"],
        n_messages=len(json.loads(row["messages"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error=row.get("error"),
    )


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """获取某个会话的消息历史"""
    if not store:
        raise HTTPException(503)
    messages = store.get_messages(session_id)
    if not messages:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return {"session_id": session_id, "messages": messages}


@app.get("/sessions/{session_id}/gates")
async def get_session_gates(session_id: str):
    """
    Phase 3: 查询某次会话的质量门结果。

    返回该会话的 Improvement 实体及其 gate_results。
    """
    if not gateway:
        raise HTTPException(503, "Quality gates not available")
    if not store:
        raise HTTPException(503, "Database not available")

    imp_id = f"imp-{session_id}"
    try:
        get_fn = gateway.engine.repository.get
        if asyncio.iscoroutinefunction(get_fn):
            improvement = await get_fn(imp_id)
        else:
            improvement = get_fn(imp_id)

        if not improvement:
            row = store.get_session(session_id)
            if not row:
                raise HTTPException(404, f"Session '{session_id}' not found")
            return {
                "session_id": session_id,
                "quality_gate": None,
                "message": "No gate results found - session may not have been gated",
            }

        gate_dict = gateway.gate_results_to_dict(improvement)
        return {
            "session_id": session_id,
            "quality_gate": gate_dict,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to retrieve gate results: {e}") from e


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    if not store:
        raise HTTPException(503)
    row = store.get_session(session_id)
    if not row:
        raise HTTPException(404)
    store.update_status(session_id, "deleted")
    return {"status": "deleted", "session_id": session_id}


# ═══════════════════════════════════════════════════════════
# Phase 8.1: SSE Streaming Endpoint
# ═══════════════════════════════════════════════════════════

from agent_prod.server.sse import stream_response


@app.get("/v1/chat/stream")
async def chat_stream(
    prompt: str,
    session_id: str = "",
    system_prompt: str = "",
):
    """流式 chat — Server-Sent Events。

    用法: curl -N "http://localhost:8000/v1/chat/stream?prompt=hello"
    """
    if not llm or not tools:
        raise HTTPException(503, "Service not ready")

    sid = session_id or f"ses_stream_{uuid.uuid4().hex[:12]}"

    async def event_generator():
        async for sse_chunk in stream_response(
            prompt=prompt,
            session_id=sid,
            llm=llm,
            tools=tools,
            max_tokens=settings.max_tokens,
            system_prompt=system_prompt or None,
        ):
            yield sse_chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════
# Phase 12: 外部 Agent 门禁评估端点
# ═══════════════════════════════════════════════════════════

from fastapi import Body

from agent_prod.trace.models import (
    AgentTrace,
    Decision,
    MetricsSnapshot,
    ToolInvocation,
    TrafficMetrics,
)


def _parse_agent_trace_from_dict(payload: dict) -> AgentTrace:
    """Parse AgentTrace from a dict payload. Reusable across endpoints.

    Raises ValueError on invalid structure.
    """
    decisions = []
    for d in payload.get("decisions", []):
        tool_calls = []
        for tc in d.get("tool_calls", []):
            tool_calls.append(ToolInvocation(
                tool_id=tc.get("tool_id", ""),
                tool_name=tc.get("tool_name", ""),
                arguments=tc.get("arguments", {}),
                result_summary=tc.get("result_summary", ""),
                success=tc.get("success", True),
                duration_ms=tc.get("duration_ms", 0.0),
            ))
        decisions.append(Decision(
            decision_id=d.get("decision_id", ""),
            model=d.get("model", ""),
            prompt_tokens=d.get("prompt_tokens", 0),
            completion_tokens=d.get("completion_tokens", 0),
            tool_calls=tool_calls,
            reasoning=d.get("reasoning", ""),
        ))

    def _parse_metrics(m: dict | None) -> MetricsSnapshot | None:
        if m is None:
            return None
        # 已知字段直传，其余未知字段进入 custom（供 Gate6 等下游使用）
        custom = dict(m.get("custom", {}))
        for extra in ("expected_answer", "expected_plan", "final_response", "user_question", "ground_truth", "reference"):
            if extra in m and extra not in custom:
                custom[extra] = m[extra]
        return MetricsSnapshot(
            latency_p95_ms=m.get("latency_p95_ms", 0.0),
            success_rate=m.get("success_rate", 1.0),
            error_rate=m.get("error_rate", 0.0),
            token_efficiency=m.get("token_efficiency", 1.0),
            custom=custom,
        )
    # 自动从 decisions 提取 agent 最终回复 (final_response)
    # 优先级: current_metrics.final_response > decisions[last].tool_calls 结果摘要
    if "final_response" not in payload.get("current_metrics", {}):
        for d in reversed(payload.get("decisions", [])):
            for tc in d.get("tool_calls", []):
                if isinstance(tc.get("result_summary"), str) and len(tc["result_summary"]) > 10:
                    cur = payload.setdefault("current_metrics", {})
                    cur["final_response"] = tc["result_summary"]
                    break
            else:
                if d.get("reasoning"):
                    cur = payload.setdefault("current_metrics", {})
                    cur["final_response"] = d["reasoning"]
            if "final_response" in payload.get("current_metrics", {}):
                break

    def _parse_traffic(t: dict | None) -> TrafficMetrics | None:
        if t is None:
            return None
        from agent_prod.trace.models import TrafficStage
        stage_str = t.get("stage", "none")
        try:
            stage = TrafficStage(stage_str)
        except ValueError:
            stage = TrafficStage.NONE
        return TrafficMetrics(
            stage=stage,
            request_count=t.get("request_count", 0),
            error_rate=t.get("error_rate", 0.0),
            latency_p95_ms=t.get("latency_p95_ms", 0.0),
            resource_usage_pct=t.get("resource_usage_pct", 0.0),
        )

    return AgentTrace(
        agent=payload.get("agent", "generic"),
        version=payload.get("version", ""),
        session_id=payload.get("session_id", f"ses_{uuid.uuid4().hex[:12]}"),
        output=payload.get("output", {}),
        output_schema=payload.get("output_schema"),
        decisions=decisions,
        baseline_metrics=_parse_metrics(payload.get("baseline_metrics")),
        current_metrics=_parse_metrics(payload.get("current_metrics")) or MetricsSnapshot(),
        traffic=_parse_traffic(payload.get("traffic")),
        traffic_percentage=payload.get("traffic_percentage", 0),
        human_approver=payload.get("human_approver", ""),
        policy_tags=payload.get("policy_tags", []),
        budget_tokens=payload.get("budget_tokens", 100_000),
        budget_time_ms=payload.get("budget_time_ms", 120_000),
        trace_id=payload.get("trace_id", ""),
        metadata=payload.get("metadata", {}),
        declared_tools=payload.get("declared_tools", []),
        auth_grant_id=payload.get("auth_grant_id", ""),
    )


@app.get("/v1/agent/types")
async def agent_types():
    """
    列出所有支持的 agent 类型及其 adapter。

    返回: {"agents": ["hermes", "claude-code", "codex", "opencode"]}
    """
    from agent_prod.trace.adapters import ADAPTER_REGISTRY
    return {
        "agents": ADAPTER_REGISTRY.list_agents(),
        "note": "Use POST /v1/agent/evaluate to evaluate agent traces",
    }


@app.post("/v1/agent/evaluate")
async def agent_evaluate(payload: dict = Body(...)):  # noqa: B008
    """
    评估任意 agent 的执行质量，返回 pass/gray/reject 决策。

    任何 agent (Hermes / Claude Code / Codex / OpenCode 等) 执行完毕后，
    将 trace 打包成 AgentTrace 格式 POST 到此端点。

    请求体结构（所有字段均为可选，缺省用默认值）:
    {
      "agent": "my-agent",              # 必填：agent 类型
      "version": "v0.2.0",
      "session_id": "ses_abc123",     # 必填：会话 ID
      "output": {"final_response": "..."},
      "decisions": [{
        "decision_id": "turn-1",
        "model": "gpt-4",
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "tool_calls": [{
          "tool_id": "t1",
          "tool_name": "search",
          "success": true
        }]
      }],
      "current_metrics": {
        "latency_p95_ms": 1200,
        "success_rate": 0.98
      },
      "baseline_metrics": {
        "latency_p95_ms": 1000,
        "success_rate": 0.99
      },
      "traffic": {"stage": "1%", "request_count": 100},
      "human_approver": "alice",
      "policy_tags": ["production"]
    }

    返回:
    {
      "agent": "my-agent",
      "session_id": "ses_abc123",
      "status": "production",         # production | rejected | ...
      "passed": true,
      "gates": [...],
      "failed_at": null,
      "fail_reason": null,
      "total_duration_ms": 42.5
    }
    """
    if not gateway:
        raise HTTPException(503, "Quality gates not available")
    if not store:
        raise HTTPException(503, "Database not available")

    # Parse trace
    try:
        trace = _parse_agent_trace_from_dict(payload)
    except Exception as e:
        raise HTTPException(400, f"Invalid trace payload: {e}") from e

    # Run gate pipeline
    result, all_passed, duration_ms = await gateway.evaluate_agent_trace(trace)

    gate_dict = gateway.gate_results_to_dict(result)

    return {
        "agent": trace.agent,
        "session_id": trace.session_id,
        "status": result.status.value,
        "passed": all_passed,
        "gates": gate_dict["gates"],
        "failed_at": gate_dict.get("failed_at"),
        "fail_reason": gate_dict.get("fail_reason"),
        "total_duration_ms": round(duration_ms, 1),
    }


# ═══════════════════════════════════════════════════════════
#  Dry-run: validate trace without executing gates
# ═══════════════════════════════════════════════════════════

@app.post("/v1/agent/evaluate/dry-run")
async def agent_dry_run(payload: dict = Body(...)):  # noqa: B008
    """
    Validate agent trace structure WITHOUT executing any gates.

    Use this during integration to check that your trace format
    is correct before running a full evaluation.

    Request: same format as POST /v1/agent/evaluate
    Response:
    {
      "valid": true,
      "agent_type": "my-agent",
      "adapter": "GenericAdapter",
      "errors": [],
      "warnings": [],
      "thresholds": {
        "gate3": {"regress_pct": 0.93, ...},
        "gate4": {"error_rate_increase": 0.02, ...}
      }
    }
    """
    from agent_prod.gates.thresholds import resolve_agent_thresholds
    from agent_prod.trace.adapters import ADAPTER_REGISTRY

    if not gateway:
        raise HTTPException(503, "Quality gates not available")
    if not store:
        raise HTTPException(503, "Database not available")

    errors: list[str] = []
    warnings: list[str] = []

    # Try to parse
    try:
        trace = _parse_agent_trace_from_dict(payload)
    except Exception as e:
        return {
            "valid": False,
            "agent_type": payload.get("agent", "generic"),
            "adapter": None,
            "errors": [f"Cannot parse trace: {e}"],
            "warnings": [],
            "thresholds": {},
        }

    # Check adapter
    agent_type = trace.agent
    adapter = ADAPTER_REGISTRY.get(agent_type)
    adapter_name = type(adapter).__name__

    # Try to convert to Improvement (catches mapping errors)
    try:
        imp = adapter.to_improvement(trace)
    except Exception as e:
        errors.append(f"Adapter {adapter_name} failed: {e}")
        return {
            "valid": False,
            "agent_type": agent_type,
            "adapter": adapter_name,
            "errors": errors,
            "warnings": warnings,
            "thresholds": {},
        }

    if imp is None:
        errors.append(f"Adapter {adapter_name} returned None — trace too sparse")
        return {
            "valid": False,
            "agent_type": agent_type,
            "adapter": adapter_name,
            "errors": errors,
            "warnings": warnings,
            "thresholds": {},
        }

    # Warnings for sparse data
    if not trace.decisions:
        warnings.append("No decisions — gate2 will skip")
    if trace.total_tokens() == 0:
        warnings.append("No token data — gate1 won't check budget accurately")
    if not trace.baseline_metrics:
        warnings.append("No baseline — gate3 will skip regression")
    if not trace.human_approver:
        warnings.append("No human_approver — gate5 may block")

    # Resolve per-agent thresholds
    config = gateway.engine.config if hasattr(gateway.engine, 'config') else {}
    thresholds = {
        "gate3": resolve_agent_thresholds("gate3", agent_type, config),
        "gate4": resolve_agent_thresholds("gate4", agent_type, config),
    }

    return {
        "valid": True,
        "agent_type": agent_type,
        "adapter": adapter_name,
        "errors": errors,
        "warnings": warnings,
        "thresholds": thresholds,
    }


# ═══════════════════════════════════════════════════════════
#  Quality Gate Stats — query evaluations from postgres
# ═══════════════════════════════════════════════════════════

@app.get("/v1/agent/stats")
async def agent_stats(agent: str = "", limit: int = 100):
    """Query quality gate evaluation stats from postgres.

    Without ?agent=: returns stats for all agents.
    With ?agent=my-agent: returns stats for that agent only.
    ?limit=N: max recent records to return (default 100).

    Returns:
    {
      "total": 2987,
      "by_status": {"production": 2909, "rejected": 77, "candidate": 1},
      "recent": [
        {"id": "imp-xxx", "status": "production", "name": "...", "created_at": "...",
         "agent": "my-agent", "gates_passed": 4, "gates_total": 4}
      ]
    }
    """
    if not gateway:
        raise HTTPException(503, "Quality gates not available")

    repo = gateway.engine.repository
    try:
        by_status: dict[str, int] = {}
        recent: list[dict] = []

        # Use repository's list() method (all repos support it)
        all_improvements = repo.list(status=None, limit=limit + 100, offset=0)
        if asyncio.iscoroutinefunction(repo.list):
            all_improvements = await all_improvements

        for imp in all_improvements:
            status = imp.status.value if hasattr(imp.status, 'value') else str(imp.status)
            by_status[status] = by_status.get(status, 0) + 1

            if len(recent) < limit:
                agent_name = imp.metadata.get("agent", "unknown") if imp.metadata else "unknown"
                gates_passed = sum(1 for r in imp.gate_results if r.passed)
                recent.append({
                    "id": imp.id,
                    "status": status,
                    "name": imp.name,
                    "agent": agent_name,
                    "gates_passed": gates_passed,
                    "gates_total": len(imp.gate_results),
                    "created_at": imp.created_at.isoformat() if imp.created_at else "",
                })

        # Filter by agent if requested
        if agent:
            recent = [r for r in recent if r["agent"] == agent]

        total = sum(by_status.values())

        return {
            "total": total,
            "by_status": by_status,
            "recent": recent,
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to query stats: {e}") from e


# ═══════════════════════════════════════════════════════════
#  Thresholds query endpoint
# ═══════════════════════════════════════════════════════════

@app.post("/v1/thresholds")
async def set_thresholds(payload: dict = Body(...)):
    """POST /v1/thresholds — 热更新 per-agent 阈值，无需重启。

    {
      "agent": "my-agent",
      "gate3": {"regress_pct": 0.92, "perf_degradation_limit": 0.05},
      "gate4": {"error_rate_increase": 0.02}
    }
    """
    if not gateway:
        raise HTTPException(503, "Quality gates not available")
    config = gateway.engine.config if hasattr(gateway.engine, 'config') else {}
    if not config:
        raise HTTPException(503, "Config not loaded")
    agent = payload.get("agent", "")
    if not agent:
        raise HTTPException(400, "agent is required")
    gates_cfg = config.setdefault("gates", {})
    for gate_name in ("gate3", "gate4"):
        if gate_name in payload:
            gate_cfg = gates_cfg.setdefault(gate_name, {})
            per_agent = gate_cfg.setdefault("per_agent", {})
            per_agent[agent] = payload[gate_name]
    return {"ok": True, "agent": agent, "updated": list(payload.keys())}


@app.get("/v1/thresholds")
async def agent_thresholds(agent: str = ""):
    """
    Query current threshold configuration.

    Without ?agent=: returns all agents with per-agent thresholds.
    With ?agent=my-agent: returns gate3/gate4 thresholds for that agent.

    Returns (all agents):
    {
      "_defaults": {"gate3": {...}, "gate4": {...}},
      "agents": {
        "my-agent": {"gate3": {"regress_pct": 0.93, ...}, "gate4": {...}},
        ...
      }
    }

    Returns (single agent, ?agent=hermes):
    {
      "agent": "my-agent",
      "gate3": {"regress_pct": 0.93, ...},
      "gate4": {"error_rate_increase": 0.02, ...}
    }
    """
    from agent_prod.gates.thresholds import list_agents_with_overrides, resolve_agent_thresholds
    from agent_prod.trace.adapters import ADAPTER_REGISTRY

    if not gateway:
        raise HTTPException(503, "Quality gates not available")
    if not store:
        raise HTTPException(503, "Database not available")

    config = gateway.engine.config if hasattr(gateway.engine, 'config') else {}

    if agent:
        g3 = resolve_agent_thresholds("gate3", agent, config)
        g4 = resolve_agent_thresholds("gate4", agent, config)
        return {"agent": agent, "gate3": g3, "gate4": g4}

    # All agents
    all_agents = set(ADAPTER_REGISTRY.list_agents())
    all_agents.update(list_agents_with_overrides("gate3", config))
    all_agents.update(list_agents_with_overrides("gate4", config))

    defaults = {
        "gate3": resolve_agent_thresholds("gate3", "", config),
        "gate4": resolve_agent_thresholds("gate4", "", config),
    }

    agents_thresholds = {}
    for a in sorted(all_agents):
        agents_thresholds[a] = {
            "gate3": resolve_agent_thresholds("gate3", a, config),
            "gate4": resolve_agent_thresholds("gate4", a, config),
        }

    return {"_defaults": defaults, "agents": agents_thresholds}


# ═══════════════════════════════════════════════════════════════
#  方案 A: Gateway 工具代理 — 运行时工具准入
# ═══════════════════════════════════════════════════════════════

from agent_prod.gates.tool_executor import ToolExecutor

_tool_executor = ToolExecutor()


@app.post("/v1/tool/execute")
async def tool_execute(payload: dict = Body(...)):  # noqa: B008
    """运行时工具入口 — agent 的工具调用先过 Gate0 再执行。

    POST /v1/tool/execute
    { "agent": "claude-code", "tool_name": "terminal",
      "arguments": {"command": "git status"},
      "declared_tools": ["read_file","terminal"], "auth_grant_id": "grant-xxx" }
    """
    if not gateway or not hasattr(gateway.engine, "gate0"):
        raise HTTPException(503, "Gate0 not available")
    agent = payload.get("agent", "generic")
    tool_name = payload.get("tool_name", "")
    arguments = payload.get("arguments", {})
    declared_tools = payload.get("declared_tools", [])
    auth_grant_id = payload.get("auth_grant_id", "")
    if not tool_name:
        raise HTTPException(400, "tool_name is required")
    check = gateway.engine.gate0.check_single_tool(
        agent=agent, tool_name=tool_name, arguments=arguments,
        declared_tools=declared_tools, auth_grant_id=auth_grant_id)
    if not check["allowed"]:
        return {"allowed": False,
            "gate0": {"risk": check["risk"], "reason": check["reason"],
                       "arg_threat": check.get("arg_threat", "")},
            "result": None}
    result = _tool_executor.execute(tool_name, arguments)
    return {"allowed": True,
        "gate0": {"risk": check["risk"], "auth_source": check.get("auth_source", ""),
                   "arg_threat": check.get("arg_threat", ""),
                   "arg_reason": check.get("arg_reason", "")},
        "result": result}


# ═══════════════════════════════════════════════════════════════
#  Auth Grant API — 用户对 agent 危险操作的显式授权
# ═══════════════════════════════════════════════════════════════

@app.post("/v1/auth/grant")
async def auth_grant(payload: dict = Body(...)):
    """颁发危险操作授权。

    POST /v1/auth/grant
    {
      "agent_type": "claude-code",
      "tool_name": "terminal",
      "granted_by": "alice",
      "reason": "CI needs shell access for build",
      "ttl_seconds": 3600
    }
    """
    if not gateway or not hasattr(gateway.engine, 'auth_store'):
        raise HTTPException(503, "Auth grant store not available")

    agent_type = payload.get("agent_type", "")
    tool_name = payload.get("tool_name", "")
    if not agent_type or not tool_name:
        raise HTTPException(400, "agent_type and tool_name are required")

    grant = gateway.engine.auth_store.grant(
        agent_type=agent_type,
        tool_name=tool_name,
        granted_by=payload.get("granted_by", "api"),
        reason=payload.get("reason", ""),
        ttl_seconds=payload.get("ttl_seconds", 0),
    )
    return grant.to_dict()


@app.get("/v1/auth/grants")
async def auth_grants_list(agent_type: str = ""):
    """列出有效授权。

    GET /v1/auth/grants?agent_type=claude-code
    """
    if not gateway or not hasattr(gateway.engine, 'auth_store'):
        raise HTTPException(503, "Auth grant store not available")

    grants = gateway.engine.auth_store.list_valid(agent_type=agent_type)
    return {"grants": [g.to_dict() for g in grants]}


@app.delete("/v1/auth/grant/{grant_id}")
async def auth_revoke(grant_id: str):
    """撤销授权。

    DELETE /v1/auth/grant/{grant_id}
    """
    if not gateway or not hasattr(gateway.engine, 'auth_store'):
        raise HTTPException(503, "Auth grant store not available")

    ok = gateway.engine.auth_store.revoke(grant_id)
    if not ok:
        raise HTTPException(404, f"Grant '{grant_id}' not found")
    return {"revoked": grant_id, "ok": True}


@app.get("/v1/auth/tool-risk")
async def tool_risk_index():
    """列出所有已知工具的风险等级。

    GET /v1/auth/tool-risk
    """
    from agent_prod.gates.tool_risk import TOOL_RISK, BENIGN_TOOLS, ELEVATED_TOOLS, DANGEROUS_TOOLS
    return {
        "benign": sorted(BENIGN_TOOLS),
        "elevated": sorted(ELEVATED_TOOLS),
        "dangerous": sorted(DANGEROUS_TOOLS),
        "full_map": {k: v.value for k, v in TOOL_RISK.items()},
    }


# ═══════════════════════════════════════════════════════════════
# Gate0 观察者/拦截模式热切换
# ═══════════════════════════════════════════════════════════════


@app.post("/v1/gate0/mode")
async def gate0_set_mode(payload: dict = Body(...)):  # noqa: B008
    """
    POST /v1/gate0/mode — 热切换 Gate0 运行模式，无需重启。

    {
      "agent": "claude-code",   # ""=全局，指定agent=per-agent覆盖
      "mode": "observe"         # "enforce" | "observe"
    }

    返回当前所有生效的模式配置。
    """
    if not gateway:
        raise HTTPException(503, "Quality gates not available")
    gate0 = gateway.engine.gate0
    agent = payload.get("agent", "")
    mode = payload.get("mode", "")
    if mode not in ("enforce", "observe"):
        raise HTTPException(400, f"无效模式 '{mode}'，有效值: enforce, observe")
    try:
        result = gate0.set_mode(agent, mode)
        return {"ok": True, "mode": mode, "config": result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/v1/gate0/mode")
async def gate0_get_mode(agent: str = ""):
    """
    GET /v1/gate0/mode — 查询 Gate0 当前运行模式。

    ?agent=claude-code  查询某 agent 的生效模式
    不带 agent 返回全局 + 所有 per-agent 覆盖
    """
    if not gateway:
        raise HTTPException(503, "Quality gates not available")
    gate0 = gateway.engine.gate0
    return gate0.get_mode(agent)


@app.get("/v1/gray/status/{improvement_id}")
async def gray_status(improvement_id: str):
    """查询灰度阶梯状态。
    
    GET /v1/gray/status/{improvement_id}
    """
    if not gateway or not hasattr(gateway.engine, 'gate4'):
        raise HTTPException(503, "Gate4 not available")
    tracker = gateway.engine.gate4.tracker
    status = tracker.status(improvement_id)
    if status is None:
        raise HTTPException(404, f"No gray state for '{improvement_id}'")
    return status


# ═══════════════════════════════════════════════════════════════
# Phase 15: Transparent LLM Proxy — intercept + Gate0 pre-flight + session accumulation
# ═══════════════════════════════════════════════════════════════

import asyncio as _asyncio
import httpx as _httpx

# Proxy session management
from agent_prod.server.proxy_session import ProxySessionManager, SessionStatus
from agent_prod.server.proxy_heartbeat import HeartbeatMonitor

_proxy_session_manager: ProxySessionManager | None = None
_heartbeat_monitor: HeartbeatMonitor | None = None
_proxy_eval_task: _asyncio.Task | None = None
_proxy_eval_running = False


async def _proxy_eval_worker_v2():
    """Background worker: evaluate COMPLETED proxy sessions.

    When a session ends (end-session signal or crash timeout),
    this worker pops the session, builds a full AgentTrace from
    ALL accumulated decisions, and runs Gate1-6 once.
    """
    global _proxy_eval_running
    _proxy_eval_running = True
    while True:
        try:
            if not _proxy_session_manager:
                await _asyncio.sleep(1)
                continue

            sessions = _proxy_session_manager.pop_for_evaluation()
            for session in sessions:
                try:
                    trace_dict = session.build_final_trace()
                    trace = _parse_agent_trace_from_dict(trace_dict)
                    result, all_passed, _ = await gateway.evaluate_agent_trace(trace)

                    session.gate_result = {
                        "status": result.status.value if hasattr(result, 'status') else '?',
                        "passed": all_passed,
                        "total_duration_ms": result.total_duration_ms if hasattr(result, 'total_duration_ms') else 0,
                    }

                    # Persist result back to SQLite
                    if _proxy_session_manager:
                        _proxy_session_manager.set_gate_result(
                            session.session_id, session.gate_result,
                        )

                    logger.info(
                        "proxy-eval: session=%s agent=%s turns=%d → %s (passed=%s)",
                        session.session_id, session.agent_type,
                        session.accumulated_decisions,
                        session.gate_result["status"],
                        all_passed,
                    )
                except Exception as e:
                    logger.warning("proxy-eval: failed for session %s: %s",
                                   session.session_id, e)

            if not sessions:
                await _asyncio.sleep(0.5)
        except _asyncio.CancelledError:
            break
        except Exception:
            await _asyncio.sleep(1)


@app.on_event("startup")
async def _start_proxy_subsystems():
    """Start proxy session manager, heartbeat monitor, and eval worker."""
    global _proxy_session_manager, _heartbeat_monitor, _proxy_eval_task
    if store:
        _proxy_session_manager = ProxySessionManager(store)
    if _proxy_session_manager and gateway and settings.quality_gates_enabled:
        _heartbeat_monitor = HeartbeatMonitor(_proxy_session_manager)
        await _heartbeat_monitor.start()
        _proxy_eval_task = _asyncio.create_task(_proxy_eval_worker_v2())
        logger.info("Proxy subsystems started: session manager + heartbeat + eval worker")


async def _run_gate0_preflight(
    agent_type: str, declared_tools: list[str], tool_defs: list[dict],
    auth_grant_id: str = "",
) -> dict:
    """Run Gate0 permission check on pre-forwarding state.

    Returns {"blocked": bool, "reason": str, "details": dict}
    """
    if not gateway:
        return {"blocked": False, "reason": "No gateway", "details": {}}

    try:
        from agent_prod.gates.tool_risk import RiskLevel, TOOL_RISK

        gate0 = gateway.engine.gate0

        blocked = []
        elevated = []
        dangerous_tools = []

        for td in tool_defs:
            func = td.get("function", {})
            tname = func.get("name", "")
            risk = TOOL_RISK.get(tname)

            if risk is None:
                if gate0._block_unknown:
                    blocked.append({"tool": tname, "reason": "unknown_tool"})
                continue

            if risk == RiskLevel.DANGEROUS:
                dangerous_tools.append(tname)
            elif risk == RiskLevel.ELEVATED:
                elevated.append(tname)

        # Auth grant check for dangerous tools
        if dangerous_tools:
            has_grant = False
            if gate0._auth_store and auth_grant_id:
                grant = gate0._auth_store.check_by_id(auth_grant_id)
                if grant and grant.is_valid():
                    has_grant = (grant.agent_type == agent_type)
            if not has_grant:
                blocked.append({
                    "tool": dangerous_tools[0],
                    "reason": "dangerous_no_auth",
                    "all_dangerous": dangerous_tools,
                })

        # Elevated tools must be declared
        if elevated:
            declared_set = set(declared_tools)
            undeclared = [t for t in elevated if t not in declared_set]
            if undeclared:
                blocked.append({
                    "tool": undeclared[0],
                    "reason": "elevated_undeclared",
                    "undeclared": undeclared,
                })

        if blocked:
            return {
                "blocked": True,
                "reason": f"Gate0 blocked {len(blocked)} tool(s): "
                          f"{[b['reason'] for b in blocked]}",
                "details": {"blocked": blocked, "declared": declared_tools,
                            "dangerous": dangerous_tools, "elevated": elevated},
            }

        return {
            "blocked": False,
            "reason": f"Gate0 passed: {len(tool_defs)} tools "
                      f"({len(dangerous_tools)} dangerous, {len(elevated)} elevated)",
            "details": {"dangerous": dangerous_tools, "elevated": elevated},
        }

    except Exception as e:
        logger.warning("Gate0 preflight error: %s", e)
        return {"blocked": False, "reason": f"Gate0 error (passthrough): {e}", "details": {}}


@app.post("/v1/proxy/chat/completions")
async def proxy_chat_completions(payload: dict = Body(...)):  # noqa: B008
    """Transparent LLM proxy — Gate0 pre-flight + session accumulation.

    External agents set one env var: OPENAI_API_BASE=http://host:8765/v1/proxy
    All OpenAI-compatible params are forwarded to the real LLM.
    Gate0 checks tool safety BEFORE forwarding.
    Decisions/tool_calls are accumulated into the session.
    Full Gate1-6 evaluation runs ONCE when the session ends.

    Returns: real LLM chat completions response (OpenAI format)
    """
    _start = time.monotonic()

    # ── 1. Extract request params ──
    messages = payload.get("messages", [])
    model = payload.get("model", settings.openai_model)
    stream = payload.get("stream", False)
    tool_defs = payload.get("tools", []) or []
    tool_choice = payload.get("tool_choice")
    temperature = payload.get("temperature", 0.7)
    max_tokens = payload.get("max_tokens")
    session_id = payload.get("session_id", f"pxy_{uuid.uuid4().hex[:12]}")
    end_session = payload.get("x-end-session", False)

    # Extract declared tool names from tool definitions
    declared = sorted(set(
        t.get("function", {}).get("name", "")
        for t in tool_defs
        if t.get("function", {}).get("name")
    ))

    # Agent 类型由调用方指定，默认 "generic"
    agent_type = payload.get("agent", "").lower()
    if not agent_type or agent_type == "auto":
        agent_type = "generic"

    # ── 2. Get or create proxy session (accumulation) ──
    session = None
    if _proxy_session_manager:
        session = _proxy_session_manager.get_or_create(
            session_id, agent_type,
            version=payload.get("version", ""),
            model=model,
        )
        if tool_defs:
            session.set_declared_tools(declared, tool_defs)

    # ── 3. Gate0 pre-flight safety check ──
    if gateway and settings.quality_gates_enabled and tool_defs:
        gate0_result = await _run_gate0_preflight(
            agent_type, declared, tool_defs,
            auth_grant_id=payload.get("auth_grant_id", ""),
        )
        if gate0_result["blocked"]:
            if _proxy_session_manager and session:
                _proxy_session_manager.finalize(
                    session_id, SessionStatus.COMPLETED,
                    error=f"Gate0 blocked: {gate0_result['reason']}",
                )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "gate0_blocked",
                    "message": gate0_result["reason"],
                    "details": gate0_result["details"],
                },
            )

    # ── 4. Forward to real LLM ──
    if not llm:
        raise HTTPException(503, "LLM backend not configured")

    forward_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tool_defs:
        forward_body["tools"] = tool_defs
    if tool_choice is not None:
        forward_body["tool_choice"] = tool_choice
    if max_tokens is not None:
        forward_body["max_tokens"] = max_tokens
    if stream:
        forward_body["stream"] = True

    try:
        resp = await llm._client.post("/chat/completions", json=forward_body)
        resp.raise_for_status()
    except _httpx.HTTPStatusError as e:
        logger.error("Proxy upstream error: %s", e)
        raise HTTPException(
            e.response.status_code,
            detail=f"Upstream LLM error: {e.response.text[:500]}",
        ) from e
    except Exception as e:
        logger.error("Proxy connection error: %s", e)
        raise HTTPException(502, f"Upstream connection failed: {e}") from e

    # ── 5. Extract response data for session accumulation ──
    raw = resp.json()
    _duration = (time.monotonic() - _start) * 1000

    response_tool_calls = []
    response_content = ""
    usage = raw.get("usage", {})
    choices = raw.get("choices", [])
    for choice in choices:
        msg = choice.get("message", {})
        if msg.get("content"):
            response_content = msg["content"]
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": fn.get("arguments", "")}
            response_tool_calls.append({
                "tool_id": tc.get("id", ""),
                "tool_name": fn.get("name", ""),
                "arguments": args,
                "success": True,
                "duration_ms": 0,
            })

    # ── 6. Accumulate into session (not per-request eval) ──
    if _proxy_session_manager and session:
        session.record_turn(
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            tool_calls=response_tool_calls,
            latency_ms=_duration,
        )
        if response_content:
            session.final_output = response_content

        # If end-session signal, finalize for evaluation
        if end_session:
            _proxy_session_manager.finalize(
                session_id, SessionStatus.COMPLETED,
                output=response_content,
            )

    # ── 7. Return real LLM response ──
    return raw


@app.post("/v1/proxy/messages")
async def proxy_anthropic_messages(payload: dict = Body(...)):  # noqa: B008
    """Anthropic Messages API → OpenAI proxy.

    Claude Code sets ANTHROPIC_BASE_URL=http://host:8765/v1/proxy
    and all LLM calls (POST /v1/messages) go through here.

    Gate0 pre-flight is applied to tool definitions before forwarding.
    Decisions are accumulated into the session for full Gate1-6 evaluation.
    """
    from agent_prod.server.anthropic_proxy import (
        anthropic_request_to_openai,
        anthropic_tools_to_openai,
        openai_to_anthropic_response,
        openai_tools_to_declared,
    )

    _start = time.monotonic()

    # ── 1. Extract Anthropic-format request params ──
    model = payload.get("model", settings.openai_model)
    stream = payload.get("stream", False)
    tool_defs = payload.get("tools", []) or []
    session_id = payload.get("session_id", f"pxy_{uuid.uuid4().hex[:12]}")
    end_session = payload.get("x-end-session", False)

    declared = openai_tools_to_declared(
        anthropic_tools_to_openai(tool_defs)
    ) if tool_defs else []

    agent_type = payload.get("agent", "").lower()
    if not agent_type or agent_type == "auto":
        agent_type = "generic"

    # ── 2. Get or create proxy session ──
    session = None
    if _proxy_session_manager:
        session = _proxy_session_manager.get_or_create(
            session_id, agent_type,
            version=payload.get("version", ""),
            model=model,
        )
        if tool_defs:
            session.set_declared_tools(declared, tool_defs)

    # ── 3. Gate0 pre-flight ──
    if gateway and settings.quality_gates_enabled and tool_defs:
        gate0_result = await _run_gate0_preflight(
            agent_type, declared,
            anthropic_tools_to_openai(tool_defs),
            auth_grant_id=payload.get("auth_grant_id", ""),
        )
        if gate0_result["blocked"]:
            if _proxy_session_manager and session:
                _proxy_session_manager.finalize(
                    session_id, SessionStatus.COMPLETED,
                    error=f"Gate0 blocked: {gate0_result['reason']}",
                )
            # Return Anthropic-format error
            return JSONResponse(
                status_code=403,
                content={
                    "type": "error",
                    "error": {
                        "type": "permission_error",
                        "message": gate0_result["reason"],
                    },
                },
            )

    # ── 4. Convert to OpenAI and forward ──
    if not llm:
        raise HTTPException(503, "LLM backend not configured")

    # Convert request format
    openai_body = anthropic_request_to_openai(payload)

    # Use server's model if upstream doesn't support Anthropic model names
    if settings.openai_model:
        openai_body["model"] = settings.openai_model

    # Disable streaming for now (simpler; Claude Code falls back gracefully)
    openai_body.pop("stream", None)

    try:
        resp = await llm._client.post("/chat/completions", json=openai_body)
        resp.raise_for_status()
    except _httpx.HTTPStatusError as e:
        logger.error("Proxy upstream error (anthropic): %s", e)
        raise HTTPException(
            e.response.status_code,
            detail=f"Upstream LLM error: {e.response.text[:500]}",
        ) from e
    except Exception as e:
        logger.error("Proxy connection error (anthropic): %s", e)
        raise HTTPException(502, f"Upstream connection failed: {e}") from e

    # ── 5. Convert response back to Anthropic format ──
    raw_openai = resp.json()
    _duration = (time.monotonic() - _start) * 1000
    anthropic_response = openai_to_anthropic_response(raw_openai)

    # ── 6. Extract tool calls and content for session accumulation ──
    response_tool_calls = []
    response_content = ""
    usage = raw_openai.get("usage", {})

    for block in anthropic_response.get("content", []):
        if block.get("type") == "text":
            response_content = block.get("text", "")
        elif block.get("type") == "tool_use":
            response_tool_calls.append({
                "tool_id": block.get("id", ""),
                "tool_name": block.get("name", ""),
                "arguments": block.get("input", {}),
                "success": True,
                "duration_ms": 0,
            })

    # ── 7. Accumulate into session ──
    if _proxy_session_manager and session:
        session.record_turn(
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            tool_calls=response_tool_calls,
            latency_ms=_duration,
        )
        if response_content:
            session.final_output = response_content

        if end_session:
            _proxy_session_manager.finalize(
                session_id, SessionStatus.COMPLETED,
                output=response_content,
            )

    # ── 8. Return Anthropic-format response ──
    return anthropic_response


@app.post("/v2/proxy/chat/completions")
async def proxy_chat_completions_v2(payload: dict = Body(...)):  # noqa: B008
    """v2 alias for transparent proxy — no deprecation headers."""
    return await proxy_chat_completions(payload)


@app.post("/v2/proxy/messages")
async def proxy_anthropic_messages_v2(payload: dict = Body(...)):  # noqa: B008
    """v2 alias for Anthropic proxy — no deprecation headers."""
    return await proxy_anthropic_messages(payload)


# ═══════════════════════════════════════════════════════════════
#  Proxy Session Management Endpoints
# ═══════════════════════════════════════════════════════════════


@app.post("/v1/proxy/register")
async def proxy_register(payload: dict = Body(...)):  # noqa: B008
    """Register a proxy agent session explicitly.

    Claude Code (or any agent) calls this at startup to announce itself.
    The agent sets ``modelBaseUrl`` to point to the proxy, but also calls
    this once to declare its identity.

    Request:
    {
      "agent": "claude-code",
      "version": "0.1.0",
      "session_id": "pxy_abc...",  // optional
      "declared_tools": ["Read", "Write", "Bash", ...],
      "model": "claude-sonnet-4-20250514"
    }

    Response:
    {
      "session_id": "pxy_abc...",
      "status": "registered",
      "proxy_url": "/v1/proxy/chat/completions"
    }
    """
    if not _proxy_session_manager:
        raise HTTPException(503, "Proxy session manager not available")

    agent_type = payload.get("agent", "generic")
    version = payload.get("version", "")
    session_id = payload.get("session_id", f"pxy_{uuid.uuid4().hex[:12]}")
    declared_tools = payload.get("declared_tools", [])
    model = payload.get("model", settings.openai_model)

    _proxy_session_manager.get_or_create(
        session_id, agent_type, version=version, model=model,
    )

    return {
        "session_id": session_id,
        "status": "registered",
        "proxy_url": "/v1/proxy/chat/completions",
    }


@app.post("/v1/proxy/heartbeat")
async def proxy_heartbeat(payload: dict = Body(...)):  # noqa: B008
    """Session heartbeat — keeps the session alive.

    Claude Code calls this periodically to signal the window is still alive.
    If N heartbeats are missed, the session is marked as CRASHED.

    Request:
    {
      "session_id": "pxy_abc...",
      "status": "active"  // optional: "completed" ends the session
    }

    Response:
    {
      "session_id": "pxy_abc...",
      "status": "active",
      "last_seen": 1234567890.0,
      "decisions_accumulated": 42
    }
    """
    if not _proxy_session_manager:
        raise HTTPException(503, "Proxy session manager not available")

    session_id = payload.get("session_id", "")
    if not session_id:
        raise HTTPException(400, "session_id is required")

    session = _proxy_session_manager.get(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")

    if payload.get("status") == "completed":
        _proxy_session_manager.finalize(session_id, SessionStatus.COMPLETED)

    return {
        "session_id": session_id,
        "status": session.status.value,
        "last_seen": session.last_seen,
        "decisions_accumulated": len(session.decisions),
    }


@app.get("/v1/proxy/sessions")
async def proxy_sessions_list(agent: str = ""):
    """List all proxy sessions — active, completed, crashed.

    This is the dashboard/status endpoint for viewing all monitored windows.
    """
    if not _proxy_session_manager:
        return {"total": 0, "active": 0, "completed": 0, "crashed": 0, "sessions": []}

    all_sessions = _proxy_session_manager.list_all()
    if agent:
        all_sessions = [s for s in all_sessions if s.get("agent_type") == agent]

    by_status: dict[str, int] = {}
    for s in all_sessions:
        st = s.get("status", "unknown")
        by_status[st] = by_status.get(st, 0) + 1

    return {
        "total": len(all_sessions),
        **by_status,
        "sessions": all_sessions,
    }


@app.get("/v1/proxy/sessions/{session_id}")
async def proxy_session_detail(session_id: str):
    """Get detailed info for a specific proxy session including gate results."""
    if not _proxy_session_manager:
        raise HTTPException(503, "Proxy session manager not available")

    session = _proxy_session_manager.get(session_id)
    if not session:
        # Fall back to persisted data
        if store:
            rows = store.list_proxy_sessions(limit=1000)
            for r in rows:
                if r["id"] == session_id:
                    return r
        raise HTTPException(404, f"Session '{session_id}' not found")

    return session.to_dict()


@app.delete("/v1/proxy/sessions/{session_id}")
async def delete_proxy_session(session_id: str):
    """Remove a proxy session from tracking."""
    if not _proxy_session_manager:
        raise HTTPException(503, "Proxy session manager not available")
    _proxy_session_manager.remove(session_id)
    return {"status": "deleted", "session_id": session_id}


# ═══════════════════════════════════════════════════════════════
#  /v2/ Route Aliases (backward-compatible, no deprecation headers)
# ═══════════════════════════════════════════════════════════════

_v2_routes = [
    # (path, handler, method)
    ("/v2/agent/types",              agent_types,        "GET"),
    ("/v2/agent/evaluate",           agent_evaluate,     "POST"),
    ("/v2/agent/evaluate/dry-run",   agent_dry_run,      "POST"),
    ("/v2/thresholds",               agent_thresholds,   "GET"),
    ("/v2/thresholds",               set_thresholds,     "POST"),
    ("/v2/tool/execute",             tool_execute,       "POST"),
    ("/v2/auth/grant",               auth_grant,         "POST"),
    ("/v2/auth/grants",              auth_grants_list,   "GET"),
    ("/v2/auth/grant/{grant_id}",    auth_revoke,        "DELETE"),
    ("/v2/auth/tool-risk",           tool_risk_index,    "GET"),
    ("/v2/gray/status/{improvement_id}", gray_status,    "GET"),
    ("/v2/gate0/mode",               gate0_set_mode,     "POST"),
    ("/v2/gate0/mode",               gate0_get_mode,     "GET"),
]

for _path, _handler, _method in _v2_routes:
    if _method == "POST":
        app.post(_path)(_handler)
    elif _method == "DELETE":
        app.delete(_path)(_handler)
    else:
        app.get(_path)(_handler)
