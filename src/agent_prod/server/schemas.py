"""FastAPI 请求/响应模型。"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    messages: list[dict] = Field(..., description="消息历史，最后一条必须是 user role")
    session_id: str = Field(default="", description="留空自动创建新会话")
    stream: bool = Field(default=False, description="是否启用流式响应")
    system_prompt: str | None = None


class ToolCallOut(BaseModel):
    id: str
    name: str
    arguments: dict


class ChatChoice(BaseModel):
    index: int = 0
    content: str | None = None
    tool_calls: list[ToolCallOut] = []
    finish_reason: str = "stop"


class GateResultOut(BaseModel):
    """单道门结果"""
    gate: str
    passed: bool
    reason: str = ""
    duration_ms: float = 0.0


class QualityGateResult(BaseModel):
    """质量门全流程结果"""
    status: str = "candidate"        # candidate | gate1_passed | ... | production | rejected
    passed: bool = False             # 是否全部通过
    gates: list[GateResultOut] = []  # 各道门详细结果
    failed_at: str | None = None     # 失败在哪个门
    fail_reason: str | None = None   # 失败原因


class ChatResponse(BaseModel):
    id: str
    session_id: str
    choices: list[ChatChoice]
    usage: dict = Field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    quality_gate: QualityGateResult | None = None  # Phase 3: 质量门结果


class SessionInfo(BaseModel):
    id: str
    status: str
    n_messages: int
    created_at: str
    updated_at: str
    error: str | None = None
    quality_gate: QualityGateResult | None = None  # Phase 3: 关联的门禁结果


class HealthResponse(BaseModel):
    status: str = "ok"
    model: str = ""
    sessions_active: int = 0
    quality_gates: bool = False
    repository: bool = False
    auth_enabled: bool = False
    rate_limit_enabled: bool = False
    watchdog_active: bool = False
    gate1_degraded: bool = False
