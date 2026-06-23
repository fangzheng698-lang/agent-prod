"""
质量门编排引擎
Phase 1: 超时/重试/异常包裹 + 结构化日志 + YAML 配置加载
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Optional

import yaml

from .models import (
    GateResult, GateName, Improvement, ImprovementStatus,
)
from .gate1_execution import Gate1Execution, Gate1Config
from .gate2_trace import Gate2TraceIntegrity
from .gate3_regression import Gate3Regression, Gate3Config
from .gate4_gray import Gate4GrayRelease, Gate4Config
from .gate5_audit import Gate5ReleaseAudit
from .repository import (
    ImprovementRepository, MemoryRepository, FileRepository,
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


# ── 编排引擎 ──────────────────────────────────────────────────

class QualityGateEngine:
    """质量门主控引擎 — Phase 1: 异常安全 + 持久化 + 可观测"""

    def __init__(
        self,
        gate1_config: Optional[Gate1Config] = None,
        gate3_config: Optional[Gate3Config] = None,
        gate4_config: Optional[Gate4Config] = None,
        use_otel: bool = False,
        repository: Optional[ImprovementRepository] = None,
        config: Optional[dict] = None,
        gate_timeout_seconds: float = 60.0,
    ):
        self.repository = repository or MemoryRepository()
        self.config = config or {}
        self.gate_timeout = gate_timeout_seconds

        # 初始化各门
        self.gate1 = Gate1Execution(gate1_config or Gate1Config.from_yaml(self.config))
        self.gate2 = Gate2TraceIntegrity.from_yaml(self.config) if not use_otel \
                     else Gate2TraceIntegrity(use_otel=True)
        self.gate3 = Gate3Regression(gate3_config or Gate3Config.from_yaml(self.config))
        self.gate4 = Gate4GrayRelease(gate4_config or Gate4Config.from_yaml(self.config))
        self.gate5 = Gate5ReleaseAudit()

    @classmethod
    def from_yaml(cls, config_path: str | Path | None = None) -> "QualityGateEngine":
        """从 YAML 配置文件创建引擎"""
        config = load_config(config_path)
        repo = create_repository(config)
        gate_timeout = config.get("gate_timeout_seconds", 60.0)

        gate1_cfg = Gate1Config.from_yaml(config)
        gate3_cfg = Gate3Config.from_yaml(config)
        gate4_cfg = Gate4Config.from_yaml(config)

        return cls(
            gate1_config=gate1_cfg,
            gate3_config=gate3_cfg,
            gate4_config=gate4_cfg,
            repository=repo,
            config=config,
            gate_timeout_seconds=gate_timeout,
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

    def run_pipeline(self, improvement: Improvement,
                     human_approver: str = "",
                     persist: bool = True) -> Improvement:
        """
        全流程：Gate1 → Gate2 → Gate3 → Gate4 → Gate5
        每道门有超时/异常保护，失败立即回滚
        每步结果持久化（可选）
        """
        pipeline_start = time.time()

        # 如果指定了审批人，先填入
        if human_approver:
            self.gate5.approve(improvement, human_approver)

        pipeline = [
            (GateName.GATE1, self.gate1.verify, self.gate1.rollback),
            (GateName.GATE2, self.gate2.verify, self.gate2.rollback),
            (GateName.GATE3, self.gate3.verify, self.gate3.rollback),
            (GateName.GATE4, self.gate4.verify, self.gate4.rollback),
            (GateName.GATE5, self.gate5.verify, self.gate5.rollback),
        ]

        gates_passed = 0
        for gate_name, verify_fn, rollback_fn in pipeline:
            # 超时保护执行
            result = self._run_with_timeout(
                gate_name, verify_fn, improvement, self.gate_timeout
            )
            improvement.add_result(result)

            # 记录执行日志
            _log_gate(
                gate_name=gate_name.value if hasattr(gate_name, 'value') else str(gate_name),
                passed=result.passed,
                duration_ms=result.duration_ms,
                improvement_id=improvement.id,
                details={"reason": result.reason} if not result.passed else None,
            )

            if not result.passed:
                # 回滚（不回滚失败也不抛）
                self._run_rollback(rollback_fn, improvement)
                improvement.status = ImprovementStatus.REJECTED

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
                return improvement

            gates_passed += 1

        # 全部通过
        improvement.status = ImprovementStatus.PRODUCTION

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
        return improvement

    def run_gate(self, improvement: Improvement, gate_name: GateName,
                 persist: bool = True) -> GateResult:
        """单道门执行（带超时/异常保护）"""
        gate_map = {
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
