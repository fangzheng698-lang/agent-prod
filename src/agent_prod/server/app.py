"""FastAPI 应用入口。生产就绪的 Agent API 服务。

Phase 3: Quality Gates 中间件集成。
每次 agent 执行完成后，结果经过 5 道门（执行/轨迹/回归/灰度/审计）
验证后返回用户，不通过的结果标记 REJECTED。
"""

from __future__ import annotations
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response

from agent_prod.server.config import settings
from agent_prod.agent.llm import LLMClient
from agent_prod.agent.tools import ToolRegistry
from agent_prod.observability.metrics import get_registry
from agent_prod.agent.runtime import AgentRuntime
from agent_prod.server.state import StateStore
from agent_prod.server.schemas import (
    ChatRequest, ChatResponse, ChatChoice, ToolCallOut,
    SessionInfo, HealthResponse,
    GateResultOut, QualityGateResult,
)
from agent_prod.gateway.gateway import QualityGateGateway


# ── 全局状态（在 lifespan 中初始化） ──

llm: LLMClient | None = None
tools: ToolRegistry | None = None
store: StateStore | None = None
gateway: QualityGateGateway | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化，关闭时清理"""
    global llm, tools, store, gateway

    # 启动
    llm = LLMClient(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
    )
    tools = ToolRegistry()

    # 注册内置工具（业务工具外部注入）
    from tools.calculator import CalculatorTool
    tools.register(CalculatorTool())

    # ── Phase 8.2: 注册扩展工具 ──
    from agent_prod.agent.tools import register_extended_tools
    register_extended_tools(tools)

    store = StateStore(settings.database_url)

    # ── Phase 3: Quality Gates 引擎 ──────────────────────────────
    gateway_enabled = settings.quality_gates_enabled
    try:
        if settings.quality_gates_mode == "production":
            gateway = QualityGateGateway.from_config(settings.quality_gates_config)
        else:
            gateway = QualityGateGateway.memory()
        if gateway_enabled:
            print(f"  Quality Gates: ENABLED ({settings.quality_gates_mode})")
            print(f"    Repository: {type(gateway.engine.repository).__name__}")
    except Exception as e:
        print(f"  Quality Gates: DEGRADED — {e}")
        gateway = QualityGateGateway.memory()

    print(f"  Agent API ready: {settings.openai_model} @ {settings.openai_base_url}")
    print(f"  Tools: {[t.name for t in tools._tools.values()]}")
    print(f"  Database: {settings.database_url}")

    yield

    # 关闭
    if llm:
        await llm.close()


app = FastAPI(
    title="Agent API",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════
# API 路由
# ═══════════════════════════════════════

@app.get("/health", response_model=HealthResponse)
async def health():
    """健康检查"""
    active = 0
    if store:
        sessions = store.list_sessions(limit=100)
        active = sum(1 for s in sessions if s["status"] == "active")
    return HealthResponse(
        status="ok",
        model=settings.openai_model,
        sessions_active=active,
        quality_gates=gateway is not None and settings.quality_gates_enabled,
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
    if not llm or not store:
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
    from agent_prod.lifecycle.task_state import TaskRun, InvalidTransition
    task_run = TaskRun(session_id=session_id)
    task_run.mark_running()

    try:
        final_messages, turns = await runtime.run(messages)
    except Exception as e:
        task_run.mark_error(str(e))
        store.update_status(session_id, "error", str(e))
        raise HTTPException(500, f"Agent runtime error: {e}")

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
        raise HTTPException(500, f"Failed to retrieve gate results: {e}")


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
