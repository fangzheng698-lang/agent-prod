"""
质量门编排引擎
Phase 1: 超时/重试/异常包裹 + 结构化日志 + YAML 配置加载
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

import yaml

from .alerts import AlertDispatcher, AlertPayload, create_dispatcher_from_config
from .auth_grants import AuthGrantStore

# ── 全局 metrics 注册表 (Prometheus) ───────────────────────────
try:
    from agent_prod.observability.metrics import get_registry
    _metrics = get_registry()
    _pipeline_total = _metrics.labeled_counter(
        "agent_prod_pipeline_total",
        "Total pipeline evaluations",
        ["status", "agent"],
    )
    _gate_duration = _metrics.histogram(
        "agent_prod_gate_duration_ms",
        "Per-gate execution duration (ms)",
        buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000, 10000, 30000],
    )
    _pipeline_duration = _metrics.histogram(
        "agent_prod_pipeline_duration_ms",
        "Full pipeline execution duration (ms)",
        buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 5000, 10000, 30000, 60000],
    )
    _gates_passed_gauge = _metrics.gauge(
        "agent_prod_gates_passed",
        "Number of gates passed in last evaluation",
    )
    _rejections = _metrics.labeled_counter(
        "agent_prod_rejections_total",
        "Rejection count by gate",
        ["gate"],
    )
    _degraded_gauge = _metrics.gauge(
        "agent_prod_gate1_degraded",
        "Gate1 circuit breaker degraded (1=degraded)",
    )
    METRICS_AVAILABLE = True
except Exception:
    METRICS_AVAILABLE = False

from .gate0_permission import Gate0Permission
from .gate1_execution import Gate1Config, Gate1Execution
from .gate2_trace import Gate2TraceIntegrity
from .gate3_regression import Gate3Config, Gate3Regression
from .gate4_gray import Gate4Config, Gate4GrayRelease
from .gate5_audit import Gate5ReleaseAudit
from .gate6_answer_quality import Gate6AnswerQuality, Gate6Config
from .gate7_execution_consistency import Gate7ExecutionConsistency
from . import tool_risk  # 工具风险分类配置
from .config_schema import validate_config, ConfigSchema
from .errors import AppError, ErrorCode
from .models import (
    GateName,
    GateResult,
    Improvement,
    ImprovementStatus,
)
from .repository import (
    FileRepository,
    ImprovementRepository,
    MemoryRepository,
)

logger = logging.getLogger(__name__)

# ── 结构化日志尝试 ──────────────────────────────────────────────

try:
    import structlog
    _STRUCTLOG = True
    # 如果 structlog 可用，用它的 get_logger
    structlog_logger = structlog.get_logger("quality_gates")
except ImportError:
    _STRUCTLOG = False
    structlog_logger = logger


def _log_gate(gate_name: str, passed: bool, duration_ms: float,
              improvement_id: str, details: dict | None = None):
    """结构化日志记录门禁结果"""
    event = {
        "event": "gate_executed",
        "gate": gate_name,
        "passed": passed,
        "duration_ms": round(duration_ms, 1),
        "improvement_id": improvement_id,
    }
    if details:
        event["details"] = details

    if _STRUCTLOG:
        structlog_logger.info(**event)
    else:
        status = "PASS" if passed else "FAIL"
        logger.info("[%s] %s (%.0fms) — %s",
                    status, gate_name, duration_ms, improvement_id)


def _log_pipeline(improvement_id: str, status: str, gates_passed: int,
                  total_gates: int, duration_ms: float):
    """结构化日志记录流水线结果"""
    event = {
        "event": "pipeline_completed",
        "improvement_id": improvement_id,
        "status": status,
        "gates_passed": gates_passed,
        "total_gates": total_gates,
        "duration_ms": round(duration_ms, 1),
    }
    if _STRUCTLOG:
        structlog_logger.info(**event)
    else:
        logger.info("Pipeline %s: %d/%d gates passed (%.0fms) — %s",
                    status, gates_passed, total_gates, duration_ms, improvement_id)


# ── 配置加载 ──────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(config_path: str | Path | None = None) -> dict:
    """加载 YAML 配置，不存在时返回空字典"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.warning("Config file not found: %s, using defaults", path)
        return {}
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    logger.info("Loaded config from %s", path)
    return config


def create_repository(config: dict | None = None) -> ImprovementRepository:
    """根据配置创建持久化仓库"""
    if not config:
        return MemoryRepository()

    storage = config.get("storage", {})
    backend = storage.get("backend", "memory")

    if backend == "file":
        file_path = storage.get("file_path", "/var/lib/quality_gates/improvements.json")
        logger.info("Using FileRepository: %s", file_path)
        return FileRepository(file_path)
    elif backend == "postgres":
        dsn = storage.get("postgres", {}).get("dsn", "")
        pool_size = storage.get("postgres", {}).get("pool_size", 5)
        logger.info("Using PostgresRepository: %s", dsn)
        from .repository import PostgresRepository
        return PostgresRepository(dsn=dsn, pool_size=pool_size)

    logger.info("Using MemoryRepository")
    return MemoryRepository()


def _evolve_baseline(improvement: Improvement, repository) -> bool:
    """基线自动演进 — PRODUCTION 的 candidate_output 覆盖为新基线.

    将此次 PRODUCTION 的决策数据和输出指标写入 improvement 的
    baseline_output，下次同 agent 的 Gate3 回归以此为新基准。
    """
    # 从已有 candidate_output 中提取指标
    candidate = improvement.candidate_output or {}

    # 提取 decisions 供归因引擎使用
    decisions = candidate.get("_decisions", [])
    if not decisions:
        # 尝试从 improvement.metadata 提取
        decisions = improvement.metadata.get("_decisions", [])

    baseline = {
        "latency_p95_ms": candidate.get("latency_p95_ms", 0),
        "success_rate": candidate.get("success_rate", 1.0),
        "error_rate": candidate.get("error_rate", 0.0),
        "token_efficiency": candidate.get("token_efficiency", 1.0),
        "_decisions": decisions,
        "_evolved_from": improvement.id,
        "_evolved_at": improvement.updated_at.isoformat() if improvement.updated_at else "",
    }

    # 保留 custom 中非内部字段
    custom = candidate.get("custom", {})
    for k, v in custom.items():
        if not k.startswith("_"):
            baseline.setdefault(k, v)

    # 拷贝 Gate6 checklist 维度分（供下次 Gate3 漂移检测）
    for k, v in candidate.items():
        if k.startswith("gate6_checklist_") and isinstance(v, (int, float)):
            baseline[k] = v

    improvement.baseline_output = baseline
    logger.info(
        "Baseline evolved: %s (agent=%s, latency=%.0fms, decisions=%d)",
        improvement.id,
        improvement.metadata.get("agent", "unknown"),
        baseline.get("latency_p95_ms", 0),
        len(decisions),
    )
    return True


def _generate_fix_prompt(improvement: Improvement) -> None:
    """自动修复提示 — 从归因/错误分类结果拼接修复指令.

    写入 improvement.metadata 供调用方获取，不改变管道结果。
    """
    parts = []

    # Gate3 归因
    attr_prompt = improvement.metadata.get("attribution_fix_prompt", "")
    if attr_prompt:
        parts.append(attr_prompt)

    # Gate6 错误分类
    last_result = None
    for gr in reversed(improvement.gate_results):
        if gr.gate_name.value == "gate6_answer_quality":
            last_result = gr
            break

    if last_result and not last_result.passed:
        g6_details = last_result.details
        ec = g6_details.get("error_class", "unknown")
        fix = g6_details.get("fix_suggestion", "")
        if fix:
            parts.append(f"## Gate6 修复建议\n错误类型: {ec}\n{fix}")

    if parts:
        improvement.metadata["auto_fix_prompt"] = "\n\n---\n\n".join(parts)
        logger.info(
            "Auto-fix prompt generated for %s (%d chars)",
            improvement.id,
            len(improvement.metadata["auto_fix_prompt"]),
        )


# ── 编排引擎 ──────────────────────────────────────────────────

class QualityGateEngine:
    """质量门主控引擎 — Phase 1: 异常安全 + 持久化 + 可观测"""

    def __init__(
        self,
        gate1_config: Gate1Config | None = None,
        gate3_config: Gate3Config | None = None,
        gate4_config: Gate4Config | None = None,
        use_otel: bool = False,
        repository: ImprovementRepository | None = None,
        config: dict | None = None,
        gate_timeout_seconds: float = 60.0,
        alert_dispatcher: AlertDispatcher | None = None,
    ):
        self.repository = repository or MemoryRepository()
        self.config = config or {}
        self.gate_timeout = gate_timeout_seconds
        self.alert_dispatcher = alert_dispatcher or AlertDispatcher()

        # ── Config schema 校验 (启动时告警，不阻断) ─────────
        if self.config:
            valid, msg = validate_config(self.config)
            if not valid:
                logger.warning("Config schema validation failed: %s", msg)

        # ── 工具风险分类配置 ──────────────────────
        # 从 config.yaml 的 tools.risk / tools.aliases 加载
        tool_risk.configure(self.config)

        # ── 沙箱配置 ──────────────────────────────────
        try:
            from .tool_executor import load_sandbox_config
            load_sandbox_config(self.config)
        except Exception:
            pass  # 沙箱配置加载失败不阻断引擎启动

        # 初始化各门
        self._auth_store = AuthGrantStore()
        self.gate0 = Gate0Permission.from_yaml(self.config, self._auth_store)
        self.gate1 = Gate1Execution(gate1_config or Gate1Config.from_yaml(self.config))
        self.gate2 = Gate2TraceIntegrity.from_yaml(self.config) if not use_otel \
                     else Gate2TraceIntegrity(use_otel=True)
        self.gate3 = Gate3Regression(
            config=gate3_config or Gate3Config.from_yaml(self.config),
            raw_config=self.config if self.config else None,
            repository=self.repository,  # 使用已实例化的 repo，不是参数（可能为None）
        )
        self.gate4 = Gate4GrayRelease(
            config=gate4_config or Gate4Config.from_yaml(self.config),
            raw_config=self.config if self.config else None,
        )
        self.gate5 = Gate5ReleaseAudit()
        self.gate6 = Gate6AnswerQuality(
            config=Gate6Config.from_yaml(self.config),
        )
        self.gate7 = Gate7ExecutionConsistency(
            raw_config=self.config,
            repository=self.repository,
        )

        # ── Gate1 熔断降级 ─────────────────────────────
        gates_cfg = self.config.get("gates", {}) if self.config else {}
        gate1_cfg = gates_cfg.get("gate1", {})
        self._gate1_consecutive_threshold = gate1_cfg.get(
            "consecutive_failures_before_escalation", 3
        )
        self._gate1_cooldown_seconds = gate1_cfg.get(
            "circuit_breaker_cooldown_seconds", 60.0
        )
        self._gate1_failures: int = 0
        self._gate1_degraded_since: float | None = None

    @classmethod
    def from_yaml(cls, config_path: str | Path | None = None) -> QualityGateEngine:
        """从 YAML 配置文件创建引擎"""
        config = load_config(config_path)
        repo = create_repository(config)
        gate_timeout = config.get("gate_timeout_seconds", 60.0)

        gate1_cfg = Gate1Config.from_yaml(config)
        gate3_cfg = Gate3Config.from_yaml(config)
        gate4_cfg = Gate4Config.from_yaml(config)

        dispatcher = create_dispatcher_from_config(config)

        return cls(
            gate1_config=gate1_cfg,
            gate3_config=gate3_cfg,
            gate4_config=gate4_cfg,
            repository=repo,
            config=config,
            gate_timeout_seconds=gate_timeout,
            alert_dispatcher=dispatcher,
        )

    def _run_with_timeout(self, gate_name: str, verify_fn, improvement: Improvement,
                          timeout: float) -> GateResult:
        """在超时保护下执行单道门"""
        start = time.time()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(verify_fn, improvement)
                try:
                    result = future.result(timeout=timeout)
                except TimeoutError:
                    future.cancel()
                    return GateResult(
                        gate_name=gate_name,
                        passed=False,
                        reason=f"Gate timeout after {timeout}s",
                        details={"timeout": True, "timeout_seconds": timeout},
                        duration_ms=(time.time() - start) * 1000,
                    )
        except Exception as e:
            logger.exception("Gate %s execution failed", gate_name)
            return GateResult(
                gate_name=gate_name,
                passed=False,
                reason=f"Gate error: {e}",
                details={"error": str(e)},
                duration_ms=(time.time() - start) * 1000,
            )
        return result

    def _run_rollback(self, rollback_fn, improvement: Improvement) -> None:
        """执行回滚，不回滚失败也不抛异常"""
        try:
            rollback_fn(improvement)
        except Exception as e:
            logger.error("Rollback for %s also failed: %s",
                         improvement.id, e)

    def _build_alert_payload(self, improvement: Improvement) -> AlertPayload:
        """Build an alert payload from a rejected improvement."""
        gates_summary = [
            {
                "gate": gr.gate_name.value if hasattr(gr.gate_name, 'value') else str(gr.gate_name),
                "passed": gr.passed,
                "reason": gr.reason,
            }
            for gr in improvement.gate_results
        ]
        return AlertPayload(
            agent_type=improvement.metadata.get("agent", ""),
            agent_version=improvement.metadata.get("agent_version", ""),
            session_id=improvement.metadata.get("session_id",
                         improvement.metadata.get("trace_session", improvement.id)),
            improvement_id=improvement.id,
            improvement_name=improvement.name,
            failed_gate=improvement.fail_gate,
            fail_reason=improvement.fail_reason,
            gates_summary=gates_summary,
            status="rejected",
            metadata=improvement.metadata,
        )

    def _dispatch_alert(self, improvement: Improvement) -> None:
        """Dispatch alert on gate failure. Never raises."""
        try:
            payload = self._build_alert_payload(improvement)
            sent = self.alert_dispatcher.send(payload)
            if sent > 0:
                logger.info("Alert dispatched to %d backend(s) for %s",
                            sent, improvement.id)
        except Exception as e:
            logger.warning("Alert dispatch failed for %s: %s", improvement.id, e)

    def _gate1_is_degraded(self) -> bool:
        """Check if gate1 is in degraded mode (circuit breaker open).

        Returns True if gate1 should be skipped — the LLM endpoint has
        failed enough consecutive times that we degrade gracefully rather
        than blocking all evaluations.
        """
        if self._gate1_degraded_since is None:
            return False
        elapsed = time.monotonic() - self._gate1_degraded_since
        if elapsed >= self._gate1_cooldown_seconds:
            # Cooldown expired — retry gate1
            self._gate1_degraded_since = None
            self._gate1_failures = 0
            logger.info("Gate1 circuit breaker closed — retrying LLM calls")
            return False
        return True

    def _on_gate1_result(self, passed: bool) -> None:
        """Update circuit breaker state after a gate1 result."""
        if passed:
            self._gate1_failures = 0
            self._gate1_degraded_since = None
        else:
            self._gate1_failures += 1
            if self._gate1_failures >= self._gate1_consecutive_threshold:
                self._gate1_degraded_since = time.monotonic()
                logger.warning(
                    "Gate1 circuit breaker OPEN — %d consecutive failures, "
                    "degrading for %.0fs",
                    self._gate1_failures, self._gate1_cooldown_seconds,
                )

    @property
    def gate1_degraded(self) -> bool:
        """Public read for health endpoint."""
        return self._gate1_is_degraded()

    @property
    def auth_store(self):
        """Public access to auth grant store for API endpoints."""
        return self._auth_store

    def run_pipeline(self, improvement: Improvement,
                     human_approver: str = "",
                     persist: bool = True) -> Improvement:
        """
        全流程：Gate0 → Gate1 → Gate2 → Gate3 → Gate4 → Gate5 → Gate6
        每道门有独立超时 (gate_timeout)，全局有总体超时 (pipeline_timeout)。
        失败立即回滚并 REJECT，不推进到下一道门。

        Gate1 有熔断降级：连续失败 N 次后自动跳过 gate1，
        cooldown 结束后自动恢复。
        """
        import concurrent.futures

        pipeline_timeout = float(self.config.get("pipeline_timeout_seconds", 180.0))

        def _run():
            return self._run_pipeline_inner(improvement, human_approver, persist)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run)
            try:
                return future.result(timeout=pipeline_timeout)
            except concurrent.futures.TimeoutError:
                improvement.status = ImprovementStatus.REJECTED
                improvement.fail_gate = "pipeline"
                improvement.fail_reason = (
                    "Pipeline total timeout ({}s)".format(int(pipeline_timeout))
                    + " - REJECTED"
                )
                logger.error("Pipeline timed out after %.0fs for %s",
                             pipeline_timeout, improvement.id)
                return improvement

    def _run_pipeline_inner(self, improvement: Improvement,
                            human_approver: str, persist: bool) -> Improvement:
        """Internal pipeline execution - called by run_pipeline with timeout wrapper."""
        pipeline_start = time.time()

        # 如果指定了审批人，先填入
        if human_approver:
            self.gate5.approve(improvement, human_approver)

        # ── 初始快照：即使中途崩溃，improvement 也可被发现 ─
        if persist:
            try:
                self.repository.save(improvement)
            except Exception as e:
                logger.error("Failed to persist initial snapshot for %s: %s",
                             improvement.id, e)

        pipeline = [
            (GateName.GATE0, self.gate0.verify, self.gate0.rollback),
            (GateName.GATE7, self.gate7.verify, self.gate7.rollback),
            (GateName.GATE1, self.gate1.verify, self.gate1.rollback),
            (GateName.GATE2, self.gate2.verify, self.gate2.rollback),
            (GateName.GATE3, self.gate3.verify, self.gate3.rollback),
            (GateName.GATE4, self.gate4.verify, self.gate4.rollback),
            (GateName.GATE5, self.gate5.verify, self.gate5.rollback),
            (GateName.GATE6, self.gate6.verify, self.gate6.rollback),
        ]

        gates_passed = 0
        for gate_name, verify_fn, rollback_fn in pipeline:
            # ── Gate1 熔断降级 ─────────────────────────
            if gate_name == GateName.GATE1 and self._gate1_is_degraded():
                t0 = time.time()
                result = GateResult(
                    gate_name=GateName.GATE1,
                    passed=True,  # degraded — don't block pipeline
                    reason=f"Degraded — circuit breaker open "
                           f"({self._gate1_failures} consecutive failures, "
                           f"cooldown {self._gate1_cooldown_seconds:.0f}s)",
                    details={"degraded": True, "consecutive_failures": self._gate1_failures},
                    duration_ms=0,
                )
                improvement.add_result(result)
                _log_gate("gate1_execution", True, 0, improvement.id,
                          details={"degraded": True})
                gates_passed += 1
                continue

            # 超时保护执行
            result = self._run_with_timeout(
                gate_name, verify_fn, improvement, self.gate_timeout
            )
            improvement.add_result(result)

            # ── Prometheus metrics 记录 ──────────────────
            if METRICS_AVAILABLE:
                gate_name_str = gate_name.value if hasattr(gate_name, 'value') else str(gate_name)
                _gate_duration.observe(result.duration_ms)
                if not result.passed:
                    _rejections.inc(gate=gate_name_str)

            # 记录执行日志
            _log_gate(
                gate_name=gate_name.value if hasattr(gate_name, 'value') else str(gate_name),
                passed=result.passed,
                duration_ms=result.duration_ms,
                improvement_id=improvement.id,
                details={"reason": result.reason} if not result.passed else None,
            )

            # ── Gate1 熔断状态更新 ─────────────────────
            if gate_name == GateName.GATE1:
                self._on_gate1_result(result.passed)

            if not result.passed:
                # 回滚（不回滚失败也不抛）
                self._run_rollback(rollback_fn, improvement)
                improvement.status = ImprovementStatus.REJECTED

                # 告警推送
                self._dispatch_alert(improvement)

                # 持久化失败状态
                if persist:
                    try:
                        self.repository.save(improvement)
                    except Exception as e:
                        logger.error("Failed to persist rejected improvement %s: %s",
                                     improvement.id, e)

                total_ms = (time.time() - pipeline_start) * 1000
                _log_pipeline(
                    improvement_id=improvement.id,
                    status="REJECTED",
                    gates_passed=gates_passed,
                    total_gates=len(pipeline),
                    duration_ms=total_ms,
                )
                if METRICS_AVAILABLE:
                    _pipeline_total.inc(status="rejected", agent=improvement.metadata.get("agent", "unknown"))
                    _pipeline_duration.observe(total_ms)
                    _gates_passed_gauge.set(gates_passed)
                    _degraded_gauge.set(1 if self._gate1_is_degraded() else 0)

                # ── 自动修复提示 ──────────────────────
                if self.config and self.config.get("gates", {}).get("auto_fix", {}).get("enabled"):
                    _generate_fix_prompt(improvement)

                return improvement

            gates_passed += 1

        # 全部通过
        improvement.status = ImprovementStatus.PRODUCTION

        # ── 基线自动演进 ──────────────────────────
        baseline_updated = False
        if self.config and self.config.get("gates", {}).get("gate3", {}).get("auto_evolve_baseline", False):
            try:
                _evolve_baseline(improvement, self.repository)
                baseline_updated = True
            except Exception as e:
                logger.warning("Baseline auto-evolution failed: %s", e)

        # 持久化成功状态
        if persist:
            try:
                self.repository.save(improvement)
            except Exception as e:
                logger.error("Failed to persist production improvement %s: %s",
                             improvement.id, e)

        total_ms = (time.time() - pipeline_start) * 1000
        _log_pipeline(
            improvement_id=improvement.id,
            status="PRODUCTION",
            gates_passed=gates_passed,
            total_gates=len(pipeline),
            duration_ms=total_ms,
        )
        if METRICS_AVAILABLE:
            _pipeline_total.inc(status="production", agent=improvement.metadata.get("agent", "unknown"))
            _pipeline_duration.observe(total_ms)
            _gates_passed_gauge.set(gates_passed)
            _degraded_gauge.set(1 if self._gate1_is_degraded() else 0)
        return improvement

    def run_gate(self, improvement: Improvement, gate_name: GateName,
                 persist: bool = True) -> GateResult:
        """单道门执行（带超时/异常保护）"""
        gate_map = {
            GateName.GATE0: (self.gate0.verify, self.gate0.rollback),
            GateName.GATE1: (self.gate1.verify, self.gate1.rollback),
            GateName.GATE2: (self.gate2.verify, self.gate2.rollback),
            GateName.GATE3: (self.gate3.verify, self.gate3.rollback),
            GateName.GATE4: (self.gate4.verify, self.gate4.rollback),
            GateName.GATE5: (self.gate5.verify, self.gate5.rollback),
        }
        verify_fn, rollback_fn = gate_map.get(gate_name, (None, None))
        if not verify_fn:
            raise ValueError(f"Unknown gate: {gate_name}")

        # 超时保护执行
        result = self._run_with_timeout(
            gate_name.value if hasattr(gate_name, 'value') else str(gate_name),
            verify_fn, improvement, self.gate_timeout
        )
        improvement.add_result(result)

        _log_gate(
            gate_name=gate_name.value if hasattr(gate_name, 'value') else str(gate_name),
            passed=result.passed,
            duration_ms=result.duration_ms,
            improvement_id=improvement.id,
        )

        if not result.passed:
            self._run_rollback(rollback_fn, improvement)
            improvement.status = ImprovementStatus.REJECTED
        elif gate_name == GateName.GATE5:
            improvement.status = ImprovementStatus.PRODUCTION

        if persist:
            try:
                self.repository.save(improvement)
            except Exception as e:
                logger.error("Failed to persist improvement %s: %s",
                             improvement.id, e)

        return result
