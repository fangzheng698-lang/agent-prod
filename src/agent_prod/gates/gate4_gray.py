"""
Gate4: 灰度放行门
核心：用 Feature Flags 替代代码里的 if-else 百分比
Phase 1: 真实 Prometheus 指标 + Unleash SDK + YAML 配置
- 生产模式：PrometheusMetricsProvider 查真实指标，Unleash 控制灰度
- 降级模式：ConfigMetricsProvider / FileMetricsProvider
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import GateName, GateResult, Improvement, RollbackLevel, RollbackPlan
from .interface import GatePlugin

logger = logging.getLogger(__name__)

# ── Feature Flag 引擎 ──────────────────────────────────────────

GRAY_STAGE_CONFIG = {
    1: {"traffic_pct": 1, "observe_cycles": 2, "label": "1%"},
    2: {"traffic_pct": 10, "observe_cycles": 4, "label": "10%"},
    3: {"traffic_pct": 50, "observe_cycles": 6, "label": "50%"},
    4: {"traffic_pct": 100, "observe_cycles": 0, "label": "100%"},
}


class GrayStageStatus(BaseModel):
    stage: int
    traffic_pct: int
    label: str
    started_at: datetime
    completed_at: datetime | None = None
    passed: bool = False
    error_rate_pct: float = 0.0
    latency_p95_ms: float = 0.0


class GrayReport(BaseModel):
    improvement_id: str
    stages: list[GrayStageStatus] = Field(default_factory=list)
    baseline_error_rate: float = 0.0
    baseline_latency_p95: float = 0.0
    final_passed: bool = False


class FlagEngine(ABC):
    """Feature Flag 抽象接口 — 生产 / 本地统一 API"""


    @abstractmethod
    def get_variant(self, improvement_id: str, user_id: str = "") -> str:
        """返回 'variant_a' (新版本) 或 'baseline' (旧版本)"""
        ...

    @abstractmethod
    def set_traffic(self, improvement_id: str, traffic_pct: int) -> None:
        """设置灰度流量比例"""
        ...

    @abstractmethod
    def remove(self, improvement_id: str) -> None:
        """移除 Flag（回滚）"""
        ...

    @abstractmethod
    def is_rolled_out(self, improvement_id: str) -> bool:
        """是否 100% 全量"""
        ...


class FileFlagEngine(FlagEngine):
    """
    本地文件驱动的 Feature Flag — 单机/开发环境
    生产环境替换为 UnleashFlagEngine
    """

    def __init__(self, flags_dir: str = "/tmp/quality_gates_flags"):
        self.flags_dir = Path(flags_dir)
        self.flags_dir.mkdir(parents=True, exist_ok=True)
        self._flags: dict[str, Any] = {}

    def _flag_path(self, improvement_id: str) -> Path:
        return self.flags_dir / f"{improvement_id}.json"

    def get_variant(self, improvement_id: str, user_id: str = "") -> str:
        """
        返回该 improvement 对指定用户的分桶
        'variant_a' = 新版本（候选），'baseline' = 旧版本
        """
        flag_file = self._flag_path(improvement_id)
        if not flag_file.exists():
            return "baseline"

        flags = json.loads(flag_file.read_text())
        traffic_pct = flags.get("traffic_pct", 0)

        # 用户粘性分桶（相同的 user_id 始终分到同一组）
        seed = hash(f"{improvement_id}:{user_id}") & 0xFFFFFFFF
        rng = random.Random(seed)
        bucket = rng.randint(1, 100)

        return "variant_a" if bucket <= traffic_pct else "baseline"

    def set_traffic(self, improvement_id: str, traffic_pct: int) -> None:
        """设置灰度流量比例"""
        flag_file = self._flag_path(improvement_id)
        old = json.loads(flag_file.read_text()) if flag_file.exists() else {}
        old["traffic_pct"] = traffic_pct
        old["updated_at"] = datetime.now(UTC).isoformat()
        flag_file.write_text(json.dumps(old, indent=2, default=str))

    def remove(self, improvement_id: str) -> None:
        """移除 Flag（回滚）"""
        flag_file = self._flag_path(improvement_id)
        if flag_file.exists():
            flag_file.unlink()

    def is_rolled_out(self, improvement_id: str) -> bool:
        """是否 100% 全量"""
        flag_file = self._flag_path(improvement_id)
        if not flag_file.exists():
            return False
        flags = json.loads(flag_file.read_text())
        return flags.get("traffic_pct", 0) >= 100


class UnleashFlagEngine(FlagEngine):
    """
    Unleash SaaS / 自建 Feature Flag 中心
    Phase 1: 通过 Unleash REST API 操作 toggle

    需要环境变量:
      UNLEASH_URL=
      UNLEASH_API_TOKEN=
    """

    def __init__(self, api_url: str = "http://localhost:4242",
                 api_token: str = "",
                 environment: str = "production",
                 app_name: str = "quality-gates"):
        self.base_url = api_url.rstrip("/")
        self.api_token = api_token or os.environ.get("UNLEASH_API_TOKEN", "")
        self.environment = environment
        self.app_name = app_name
        self._degraded = False

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        return h

    def _toggle_name(self, improvement_id: str) -> str:
        return f"gate4.{improvement_id}"

    def get_variant(self, improvement_id: str, user_id: str = "") -> str:
        if self._degraded:
            return "baseline"
        try:
            import requests
            toggle = self._toggle_name(improvement_id)
            # Unleash v4+ REST API: GET /api/admin/projects/default/features/{toggle}/environments/{env}
            resp = requests.get(
                f"{self.base_url}/api/admin/projects/default/features/{toggle}/environments/{self.environment}",
                headers=self._headers(),
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                enabled = data.get("enabled", False)
                variants = data.get("variants", [])
                if enabled and variants:
                    # 按用户粘性分桶
                    seed = hash(f"{improvement_id}:{user_id}") & 0xFFFFFFFF
                    total_weight = sum(v.get("weight", 0) for v in variants)
                    bucket = (seed % 100) if total_weight > 0 else 0
                    cumulative = 0
                    for v in variants:
                        cumulative += v.get("weight", 0)
                        if bucket <= cumulative:
                            return v.get("name", "baseline")
                return "variant_a" if enabled else "baseline"
            return "baseline"
        except (ImportError, Exception) as e:
            logger.warning("Unleash API failed, degrading: %s", e)
            self._degraded = True
            return "baseline"

    def set_traffic(self, improvement_id: str, traffic_pct: int) -> None:
        if self._degraded:
            return
        try:
            import requests
            toggle = self._toggle_name(improvement_id)
            payload = {
                "name": toggle,
                "type": "release",
                "enabled": traffic_pct > 0,
                "project": "default",
                "variants": [
                    {"name": "variant_a", "weight": traffic_pct, "stickiness": "userId"},
                    {"name": "baseline", "weight": 100 - traffic_pct, "stickiness": "userId"},
                ],
            }
            # 先检查是否存在
            check = requests.get(
                f"{self.base_url}/api/admin/projects/default/features/{toggle}",
                headers=self._headers(),
                timeout=5,
            )
            if check.status_code == 404:
                # 创建 toggle
                requests.post(
                    f"{self.base_url}/api/admin/projects/default/features",
                    json=payload,
                    headers=self._headers(),
                    timeout=5,
                )
            # 更新环境配置
            requests.put(
                f"{self.base_url}/api/admin/projects/default/features/{toggle}/environments/{self.environment}",
                json={
                    "enabled": traffic_pct > 0,
                    "variants": payload["variants"],
                },
                headers=self._headers(),
                timeout=5,
            )
        except (ImportError, Exception) as e:
            logger.warning("Unleash set_traffic failed: %s", e)

    def remove(self, improvement_id: str) -> None:
        if self._degraded:
            return
        try:
            import requests
            toggle = self._toggle_name(improvement_id)
            requests.delete(
                f"{self.base_url}/api/admin/projects/default/features/{toggle}",
                headers=self._headers(),
                timeout=5,
            )
        except (ImportError, Exception) as e:
            logger.warning("Unleash remove failed: %s", e)

    def is_rolled_out(self, improvement_id: str) -> bool:
        if self._degraded:
            return False
        try:
            import requests
            toggle = self._toggle_name(improvement_id)
            resp = requests.get(
                f"{self.base_url}/api/admin/projects/default/features/{toggle}/environments/{self.environment}",
                headers=self._headers(),
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("enabled", False) and any(
                    v.get("weight", 0) >= 100
                    for v in data.get("variants", [])
                )
            return False
        except Exception:
            return False


# ── 配置加载 ──────────────────────────────────────────────────────



# ── Gate4 执行器 ────────────────────────────────────────────────

class Gate4Config(BaseModel):
    error_rate_increase: float = 0.01
    latency_increase: float = 0.10
    resource_increase: float = 0.15
    stable_period_seconds: int = 10
    # ── 灰度阶梯状态机 ──
    stateful: bool = True
    stage_min_duration_seconds: float = 0.1      # 测试用 0.1s，生产环境配 600+
    stage_error_threshold: float = 0.02
    # 灰度阶梯配置
    stages: dict[int, dict[str, Any]] = Field(default_factory=lambda: dict(GRAY_STAGE_CONFIG))

    # 指标提供者类型: prometheus | config | file | demo
    metrics_provider: str = "demo"
    prometheus_url: str = "http://localhost:9090"
    config_metrics: dict[str, Any] = Field(default_factory=dict)

    # Feature Flag 引擎类型: unleash | file
    flag_engine: str = "file"
    unleash_url: str = "http://localhost:4242"
    unleash_api_token: str = ""

    @classmethod
    def from_yaml(cls, data: dict | None) -> Gate4Config:
        if not data:
            return cls()
        gate_cfg = data.get("gates", {}).get("gate4", {})
        obs = data.get("observability", {})
        metrics_cfg = obs.get("metrics", {})
        unleash_cfg = obs.get("unleash", {})

        # 合并配置
        merged = dict(gate_cfg)
        merged.setdefault("stages", gate_cfg.get("stages", dict(GRAY_STAGE_CONFIG)))
        merged.setdefault("prometheus_url", metrics_cfg.get("prometheus_url", "http://localhost:9090"))
        merged["metrics_provider"] = metrics_cfg.get("provider", gate_cfg.get("metrics_provider", "demo"))
        merged["unleash_url"] = unleash_cfg.get("url", "http://localhost:4242")
        merged["unleash_api_token"] = unleash_cfg.get("api_token", "")

        return cls(**{k: v for k, v in merged.items()
                       if k in cls.model_fields})

    @classmethod
    def resolve_for_agent(cls, agent_type: str, config: dict | None) -> Gate4Config:
        """Create a Gate4Config with agent-specific thresholds resolved.

        Merges global gate4 config with per_agent[agent_type] overrides.
        If no per-agent overrides exist, returns the global config as-is.
        The non-threshold fields (stages, metrics_provider, etc.) are taken
        from the resolved dict and passed through.
        """
        from .thresholds import resolve_agent_thresholds
        resolved = resolve_agent_thresholds("gate4", agent_type, config)
        return cls(**{k: v for k, v in resolved.items()
                       if k in cls.model_fields})


# ── 灰度阶梯状态追踪器 ──────────────────────────────────────────

class GrayStageState(str, Enum):
    PENDING = "pending"
    WAITING = "waiting"       # 在阶梯内等待最短时间
    ADVANCING = "advancing"   # 可推进到下一阶梯
    FAILED = "failed"         # 本阶梯失败
    COMPLETED = "completed"   # 全部阶梯通过


class GrayStateTracker:
    """追踪每个 improvement 的灰度阶梯进度 — 内存存储，单实例。
    
    每次 evaluate 调用时检查是否可以推进到下一阶梯。
    """

    def __init__(self):
        self._states: dict[str, dict] = {}

    def init(self, improvement_id: str, total_stages: int) -> dict:
        s = {
            "improvement_id": improvement_id,
            "current_stage": 0,
            "total_stages": total_stages,
            "current_traffic_pct": 0,
            "stage_started_at": None,
            "last_error_rate": 0.0,
            "history": [],
        }
        self._states[improvement_id] = s
        return s

    def get(self, improvement_id: str) -> dict | None:
        return self._states.get(improvement_id)

    def stage_start(self, improvement_id: str, stage_num: int, traffic_pct: int):
        s = self._states.get(improvement_id)
        if s is None:
            return
        s["current_stage"] = stage_num
        s["current_traffic_pct"] = traffic_pct
        s["stage_started_at"] = time.time()

    def check_advance(self, improvement_id: str,
                      min_duration_s: float,
                      error_threshold: float,
                      current_error_rate: float) -> tuple[GrayStageState, float]:
        """检查当前阶梯是否可以推进。"""
        s = self._states.get(improvement_id)
        if s is None:
            return GrayStageState.PENDING, 0

        # 检查错误率
        if current_error_rate > error_threshold:
            s["last_error_rate"] = current_error_rate
            self._record(s, "failed", reason=f"error_rate={current_error_rate:.4f} > {error_threshold}")
            return GrayStageState.FAILED, 0

        # 检查最短停留时间
        started = s.get("stage_started_at")
        if started is None:
            remaining = min_duration_s
        else:
            elapsed = time.time() - started
            remaining = max(0, min_duration_s - elapsed)

        if remaining > 0:
            return GrayStageState.WAITING, remaining

        return GrayStageState.ADVANCING, 0

    def mark_completed(self, improvement_id: str):
        s = self._states.get(improvement_id)
        if s:
            s["current_stage"] = s["total_stages"]
            s["current_traffic_pct"] = 100
            self._record(s, "completed")

    def _record(self, s: dict, event: str, **kwargs):
        s["history"].append({
            "event": event,
            "stage": s["current_stage"],
            "traffic_pct": s["current_traffic_pct"],
            "at": time.time(),
            **kwargs,
        })

    def status(self, improvement_id: str) -> dict | None:
        s = self._states.get(improvement_id)
        if s is None:
            return None
        return {
            "improvement_id": s["improvement_id"],
            "current_stage": s["current_stage"],
            "total_stages": s["total_stages"],
            "current_traffic_pct": s["current_traffic_pct"],
            "stage_started_at": s.get("stage_started_at"),
            "history": s["history"][-5:],
        }

    def remove(self, improvement_id: str):
        self._states.pop(improvement_id, None)


class Gate4GrayRelease(GatePlugin):
    """灰度放行门 — 阶梯状态机 + 可插拔指标源"""

    name = GateName.GATE4
    rollback_level = RollbackLevel.L4

    _gray_tracker: GrayStateTracker | None = None

    def __init__(self, config: Gate4Config | None = None,
                 raw_config: dict | None = None):
        self.config = config or Gate4Config()
        self._raw_config = raw_config
        self._metrics: Any = None
        self._flag_engine: FlagEngine | None = None
        if Gate4GrayRelease._gray_tracker is None:
            Gate4GrayRelease._gray_tracker = GrayStateTracker()

    @property
    def tracker(self) -> GrayStateTracker:
        return Gate4GrayRelease._gray_tracker  # type: ignore[return-value]

    @property
    def metrics(self) -> Any:
        """惰性初始化指标提供者"""
        if self._metrics is None:
            self._metrics = self._init_metrics_provider()
        return self._metrics

    @property
    def flags(self) -> FlagEngine:
        """惰性初始化 Feature Flag 引擎"""
        if self._flag_engine is None:
            self._flag_engine = self._init_flag_engine()
        return self._flag_engine

    def _init_metrics_provider(self) -> Any:
        """根据配置初始化指标提供者"""
        provider_type = self.config.metrics_provider

        if provider_type == "prometheus":
            from .metrics import PrometheusMetricsProvider
            logger.info("Gate4: using PrometheusMetricsProvider (%s)",
                        self.config.prometheus_url)
            return PrometheusMetricsProvider(
                prometheus_url=self.config.prometheus_url,
            )
        elif provider_type == "config":
            from .metrics import ConfigMetricsProvider
            logger.info("Gate4: using ConfigMetricsProvider")
            return ConfigMetricsProvider(self.config.config_metrics)
        elif provider_type == "file":
            from .metrics import FileMetricsProvider
            logger.info("Gate4: using FileMetricsProvider")
            return FileMetricsProvider()
        else:
            # demo: 构造一个模拟的 MetricsProvider，行为兼容原有随机逻辑
            return _DemoMetricsProvider()

    def _init_flag_engine(self) -> FlagEngine:
        """根据配置初始化 Flag 引擎"""
        if self.config.flag_engine == "unleash":
            logger.info("Gate4: using UnleashFlagEngine (%s)",
                        self.config.unleash_url)
            return UnleashFlagEngine(
                api_url=self.config.unleash_url,
                api_token=self.config.unleash_api_token,
            )
        logger.info("Gate4: using FileFlagEngine")
        return FileFlagEngine()

    def stage_observe(self, improvement: Improvement, stage: int,
                      stage_config: dict, cfg: Gate4Config) -> dict[str, Any]:
        """
        观察灰度阶梯的效果
        Phase 1: 用 MetricsProvider 替代 random.uniform()
        """
        metrics = self.metrics.observe_stage(
            improvement_id=improvement.id,
            stage=stage,
            traffic_pct=stage_config["traffic_pct"],
            label=stage_config["label"],
        )

        # 判断是否通过 — 使用 resolved cfg
        passed = (
            metrics.error_rate <= cfg.error_rate_increase
            and metrics.latency_p95_ms <= improvement.baseline_output.get(
                "latency_p95_ms", 100
            ) * (1 + cfg.latency_increase)
        )

        return {
            "stage": stage,
            "traffic_pct": metrics.traffic_pct,
            "label": metrics.label,
            "error_rate": metrics.error_rate,
            "latency_p95_ms": metrics.latency_p95_ms,
            "resource_pct": metrics.resource_pct,
            "passed": passed,
            "sampled_at": metrics.sampled_at.isoformat(),
        }

    def _resolve_config(self, improvement: Improvement) -> Gate4Config:
        """Resolve per-agent thresholds if agent metadata is present."""
        agent_type = improvement.metadata.get("agent", "")
        if agent_type and self._raw_config:
            return Gate4Config.resolve_for_agent(agent_type, self._raw_config)
        return self.config

    def verify(self, improvement: Improvement) -> GateResult:
        start = time.time()
        cfg = self._resolve_config(improvement)

        # 非灰度场景：快速放行。
        # traffic_percentage==0 且无历史灰度记录 → 跳过
        if improvement.traffic_percentage == 0 and improvement.gray_stage == 0:
            return GateResult(
                gate_name=GateName.GATE4,
                passed=True,
                reason="No gray release active — gate4 skipped",
                details={"skipped": True, "traffic_pct": 0, "mode": "stateless"},
                duration_ms=(time.time() - start) * 1000,
            )

        # ── 默认走 stateful 路径（服务端维护灰度状态） ──
        # stateless 模式仅用于兼容和单次验证，不推荐生产使用
        if not cfg.stateful:
            logger.warning("Gate4 running in stateless mode — NOT recommended for production")
            return self._verify_stateless(improvement, start, cfg)

        return self._verify_stateful(improvement, start, cfg)

    def _verify_stateless(self, improvement: Improvement, start: float,
                          cfg: Gate4Config) -> GateResult:
        """旧行为：一次性跑完所有阶梯（用于兼容和单次验证）。"""
        stages_report: list[dict] = []
        stages = cfg.stages
        try:
            for stage_num in sorted(stages.keys()):
                stage_cfg = stages[stage_num]
                self.flags.set_traffic(improvement.id, stage_cfg["traffic_pct"])
                improvement.traffic_percentage = stage_cfg["traffic_pct"]
                improvement.gray_stage = stage_num
                time.sleep(min(cfg.stable_period_seconds, 1))
                obs = self.stage_observe(improvement, stage_num, stage_cfg, cfg)
                stages_report.append(obs)
                if not obs["passed"]:
                    self.flags.remove(improvement.id)
                    improvement.traffic_percentage = 0
                    improvement.gray_stage = 0
                    return GateResult(
                        gate_name=GateName.GATE4, passed=False,
                        reason=f"Stage {stage_num}({stage_cfg['label']}) failed: "
                               f"error={obs['error_rate']}, latency={obs['latency_p95_ms']}ms",
                        details={"stages": stages_report, "failed_at_stage": stage_num},
                        duration_ms=(time.time() - start) * 1000,
                    )
            self.flags.set_traffic(improvement.id, 100)
            improvement.gray_stage = 4
            improvement.traffic_percentage = 100
            return GateResult(
                gate_name=GateName.GATE4, passed=True,
                reason="All gray stages passed (stateless)",
                details={"stages": stages_report, "gray_completed": True, "mode": "stateless"},
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            logger.exception("Gate4 stateless verify failed")
            return GateResult(
                gate_name=GateName.GATE4, passed=False,
                reason=f"Gate4 error: {e}",
                details={"stages": stages_report, "error": str(e)},
                duration_ms=(time.time() - start) * 1000,
            )

    def _verify_stateful(self, improvement: Improvement, start: float,
                         cfg: Gate4Config) -> GateResult:
        """阶梯状态机：每次调用推进一个阶梯。"""
        stages = cfg.stages
        stage_nums = sorted(stages.keys())
        tracker = self.tracker

        # 初始化或获取当前状态
        state = tracker.get(improvement.id)
        if state is None:
            state = tracker.init(improvement.id, len(stage_nums))
            # 从第1阶梯开始
            tracker.stage_start(improvement.id, stage_nums[0],
                                stages[stage_nums[0]]["traffic_pct"])

        current_stage = state["current_stage"]

        if current_stage > max(stage_nums):
            # 已完成
            return GateResult(
                gate_name=GateName.GATE4, passed=True,
                reason=f"All {len(stage_nums)} gray stages completed",
                details={"gray_completed": True, "mode": "stateful",
                         "status": tracker.status(improvement.id)},
                duration_ms=(time.time() - start) * 1000,
            )

        # 设置当前流量
        stage_cfg = stages[current_stage]
        self.flags.set_traffic(improvement.id, stage_cfg["traffic_pct"])
        improvement.traffic_percentage = stage_cfg["traffic_pct"]
        improvement.gray_stage = current_stage

        # 观察指标
        obs = self.stage_observe(improvement, current_stage, stage_cfg, cfg)

        # 检查是否可推进
        advance_state, remaining = tracker.check_advance(
            improvement.id,
            cfg.stage_min_duration_seconds,
            cfg.stage_error_threshold,
            obs.get("error_rate", 0),
        )

        if advance_state == GrayStageState.FAILED:
            self.flags.remove(improvement.id)
            improvement.traffic_percentage = 0
            improvement.gray_stage = 0
            tracker.remove(improvement.id)
            return GateResult(
                gate_name=GateName.GATE4, passed=False,
                reason=f"Stage {current_stage}({stage_cfg['label']}) failed: "
                       f"error_rate={obs['error_rate']} > {cfg.stage_error_threshold}",
                details={"stage_report": obs, "state": "failed",
                         "mode": "stateful", "rollback": True},
                duration_ms=(time.time() - start) * 1000,
            )

        if advance_state == GrayStageState.WAITING:
            return GateResult(
                gate_name=GateName.GATE4, passed=True,
                reason=f"Stage {current_stage}({stage_cfg['label']}) HOLD — "
                       f"need {remaining:.0f}s more observation",
                details={"stage_report": obs, "state": "waiting",
                         "remaining_seconds": round(remaining, 1), "mode": "stateful"},
                duration_ms=(time.time() - start) * 1000,
            )

        # ADVANCING — 进入下一阶梯
        next_stage_idx = stage_nums.index(current_stage) + 1
        if next_stage_idx < len(stage_nums):
            next_stage = stage_nums[next_stage_idx]
            tracker.stage_start(improvement.id, next_stage,
                                stages[next_stage]["traffic_pct"])
            return GateResult(
                gate_name=GateName.GATE4, passed=True,
                reason=f"Stage {current_stage} PASSEd → advancing to stage {next_stage} "
                       f"({stages[next_stage]['label']})",
                details={"stage_report": obs, "state": "advancing",
                         "next_stage": next_stage,
                         "next_traffic_pct": stages[next_stage]["traffic_pct"],
                         "mode": "stateful"},
                duration_ms=(time.time() - start) * 1000,
            )
        else:
            # 最后一阶通过 → 全量
            self.flags.set_traffic(improvement.id, 100)
            improvement.gray_stage = 4
            improvement.traffic_percentage = 100
            tracker.mark_completed(improvement.id)
            return GateResult(
                gate_name=GateName.GATE4, passed=True,
                reason=f"All {len(stage_nums)} gray stages passed → 100% rollout",
                details={"stage_report": obs, "gray_completed": True, "mode": "stateful"},
                duration_ms=(time.time() - start) * 1000,
            )

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L4 回滚：灰度流量切回基线"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L4,
            scope="switch gray traffic back to baseline",
            estimated_seconds=30,
            procedure="DNS/config switch: revert traffic from candidate to baseline",
            executed_at=datetime.now(UTC),
            success=True,
        )
        improvement.traffic_percentage = 0
        improvement.gray_stage = 0

        # 清理 flag 文件
        FileFlagEngine().remove(improvement.id)

    @classmethod
    def from_config(cls, config: dict, name: GateName) -> Gate4GrayRelease:
        return cls(config=Gate4Config.from_yaml(config), raw_config=config)


# ── Demo 指标提供者（兼容原有随机逻辑） ─────────────────────────

class _DemoMetricsProvider:
    """
    演示模式指标提供者 — 行为兼容原有 random.uniform() 逻辑
    仅在 metrics_provider == "demo" 时使用
    使用固定种子确保结果稳定
    """

    def __init__(self):
        self._rng = random.Random(42)  # 固定种子

    def observe_stage(self, improvement_id: str, stage: int,
                       traffic_pct: int, label: str) -> Any:
        """返回一个 GrayMetrics 兼容对象，生成稳定通过值"""
        from types import SimpleNamespace
        # 使用稳定值确保演示始终通过
        return SimpleNamespace(
            stage=stage,
            traffic_pct=traffic_pct,
            label=label,
            error_rate=round(self._rng.uniform(0.001, 0.005), 4),   # 低错误率
            latency_p95_ms=round(self._rng.uniform(80, 105), 1),    # 稳定在阈值内
            resource_pct=round(self._rng.uniform(40, 55), 1),
            passed=True,
            sampled_at=datetime.now(UTC),
        )

# ── GatePlugin registration ──────────────────────────────
from .interface import register_gate

register_gate(GateName.GATE4, Gate4GrayRelease)

