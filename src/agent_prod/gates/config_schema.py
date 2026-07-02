# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""配置 Schema — Pydantic 模型校验 config.yaml 启动时检查。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Gate1Schema(BaseModel):
    execution_time_tolerance: float = 1.2
    token_tolerance: float = 1.1
    consecutive_failures_before_escalation: int = 3
    circuit_breaker_cooldown_seconds: float = 60.0


class Gate3AgentThreshold(BaseModel):
    regress_pct: float = 0.95
    perf_degradation_limit: float = 0.06


class Gate3Schema(BaseModel):
    regress_pct: float = 0.95
    perf_degradation_limit: float = 0.06
    baseline_window: int = Field(default=50, ge=1)
    deepdiff_enabled: bool = True
    per_agent: dict[str, Gate3AgentThreshold] = Field(default_factory=dict)


class Gate4Schema(BaseModel):
    error_rate_increase: float = 0.01
    latency_increase_pct: float = 0.10
    observe_cycles: int = Field(default=2, ge=0)
    per_agent: dict[str, dict[str, float]] = Field(default_factory=dict)


class Gate6Schema(BaseModel):
    enabled: bool = True
    evaluator: str = "exact-match"
    pass_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    timeout_seconds: float = 30.0
    fallback_on_timeout: str = "pass"
    llm_model: str = ""
    llm_endpoint: str = ""
    llm_api_key_env: str = "OPENAI_API_KEY"


class SandboxSchema(BaseModel):
    path_whitelist: list[str] = Field(default_factory=list)


class MetricsSchema(BaseModel):
    provider: str = "demo"
    prometheus_port: int = 9090
    prometheus_host: str = "localhost"


class PipelineSchema(BaseModel):
    pipeline_timeout_seconds: float = Field(default=180.0, ge=10.0)
    gate_timeout_seconds: float = Field(default=30.0, ge=1.0)


class ConfigSchema(BaseModel):
    """config.yaml 的完整 Schema。启动时用 model_validate() 校验。"""

    gates: dict[str, Any] = Field(default_factory=dict)
    gate1: Gate1Schema = Field(default_factory=Gate1Schema)
    gate3: Gate3Schema = Field(default_factory=Gate3Schema)
    gate4: Gate4Schema = Field(default_factory=Gate4Schema)
    gate6: Gate6Schema = Field(default_factory=Gate6Schema)
    sandbox: SandboxSchema = Field(default_factory=SandboxSchema)
    metrics: MetricsSchema = Field(default_factory=MetricsSchema)
    pipeline: PipelineSchema = Field(default_factory=PipelineSchema)
    repository: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_gates(cls, data: Any) -> Any:
        """从顶层提取 gate* 子配置 — config.yaml 可能嵌套或不嵌套。"""
        if not isinstance(data, dict):
            return data
        gates = data.get("gates", {})
        for gate_name in ("gate1", "gate3", "gate4", "gate6"):
            if gate_name in gates and gate_name not in data:
                data[gate_name] = gates[gate_name]
        return data


def validate_config(config: dict) -> tuple[bool, str]:
    """校验 config 字典。返回 (valid, error_message)。"""
    try:
        ConfigSchema.model_validate(config)
        return True, "OK"
    except Exception as e:
        return False, str(e)
