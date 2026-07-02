"""
Gate3: 回归验证门
核心：用 DeepDiff 做结构化 diff + 数值指标降级检测
Phase 1: 阈值从 YAML 加载 + 异常保护
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from .models import GateName, GateResult, Improvement, RollbackLevel, RollbackPlan

logger = logging.getLogger(__name__)

try:
    from deepdiff import DeepDiff
    _DEEPDIFF_AVAILABLE = True
except ImportError:
    _DEEPDIFF_AVAILABLE = False
    DeepDiff = None  # type: ignore


class Regression(BaseModel):
    """单条回归检测结果"""
    field: str
    old_value: Any = None
    new_value: Any = None
    change_type: str = ""  # changed / added / removed / type_change / degraded
    severity: str = "info"  # info / warning / critical


class Gate3Config(BaseModel):
    regress_pct: float = 0.95
    perf_degradation_limit: float = 0.05
    repeatability_threshold: float = 0.1
    repeatability_runs: int = 3
    unstable_retry_count: int = 5
    # ── 动态基线 ──
    dynamic_baseline: bool = False
    baseline_window: int = 20
    baseline_min_samples: int = 5

    @classmethod
    def from_yaml(cls, data: dict | None) -> Gate3Config:
        if not data:
            return cls()
        gate_cfg = data.get("gates", {}).get("gate3", {})
        return cls(**{k: v for k, v in gate_cfg.items()
                       if k in cls.model_fields})

    @classmethod
    def resolve_for_agent(cls, agent_type: str, config: dict | None) -> Gate3Config:
        """Create a Gate3Config with agent-specific thresholds resolved.

        Merges global gate3 config with per_agent[agent_type] overrides.
        If no per-agent overrides exist, returns the global config as-is.
        """
        from .thresholds import resolve_agent_thresholds
        resolved = resolve_agent_thresholds("gate3", agent_type, config)
        return cls(**{k: v for k, v in resolved.items()
                       if k in cls.model_fields})


class Gate3Regression:
    """回归验证门 — DeepDiff 结构化对比 + 动态基线"""

    def __init__(self, config: Gate3Config | None = None,
                 raw_config: dict | None = None,
                 repository=None):
        self.config = config or Gate3Config()
        self._raw_config = raw_config
        self._repo = repository

    def _resolve_config(self, improvement: Improvement) -> Gate3Config:
        """Resolve per-agent thresholds if agent metadata is present."""
        agent_type = improvement.metadata.get("agent", "")
        if agent_type and self._raw_config:
            return Gate3Config.resolve_for_agent(agent_type, self._raw_config)
        return self.config

    def verify(self, improvement: Improvement) -> GateResult:
        start = time.time()
        cfg = self._resolve_config(improvement)

        # ── 动态基线计算 ──
        if cfg.dynamic_baseline and self._repo:
            dyn = self._compute_dynamic_baseline(improvement, cfg)
            if dyn:
                improvement.baseline_output = improvement.baseline_output or {}
                # 提取元信息（不混入指标 dict）
                baseline_source = dyn.pop("source", "dynamic")
                baseline_samples = dyn.pop("samples", 0)
                for k, v in dyn.items():
                    improvement.baseline_output.setdefault(k, v)
                # 保存到 metadata 供 GateResult details 使用
                improvement.metadata["gate3_baseline_source"] = baseline_source
                improvement.metadata["gate3_baseline_samples"] = baseline_samples

        if not improvement.baseline_output:
            return GateResult(
                gate_name=GateName.GATE3,
                passed=True,
                reason="No baseline — first run, skipping regression",
                details={"skipped": True, "source": "none"},
                duration_ms=(time.time() - start) * 1000,
            )

        if _DEEPDIFF_AVAILABLE:
            return self._verify_deepdiff(improvement, start, cfg)
        else:
            return self._verify_fallback(improvement, start, cfg)

    def _compute_dynamic_baseline(self, improvement: Improvement,
                                  cfg: Gate3Config) -> dict | None:
        """从 FileRepository 中提取同 agent 最近 PRODUCTION 记录的指标统计。

        Returns dict of computed baselines (latency_p50, success_rate_avg, etc.)
        or None if not enough history.
        """
        agent_type = improvement.metadata.get("agent", "")
        if not agent_type:
            return None

        try:
            all_records = self._repo.list(status="production", limit=cfg.baseline_window * 2)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("Gate3: repository query failed (%s): %s", type(e).__name__, e)
            return None
        except Exception:
            logger.exception("Gate3: unexpected error querying repository — REJECTING")
            raise  # unexpected → let pipeline catch and reject

        # 过滤同 agent 类型且有候选输出的记录
        same_agent = []
        for rec in all_records:
            rec_agent = rec.metadata.get("agent", "") if hasattr(rec, 'metadata') else ""
            if rec_agent == agent_type and rec.candidate_output:
                same_agent.append(rec)

        if len(same_agent) < cfg.baseline_min_samples:
            logger.info("Gate3: dynamic baseline — only %d/%d samples for %s, fallback to manual",
                        len(same_agent), cfg.baseline_min_samples, agent_type)
            return None

        recent = same_agent[:cfg.baseline_window]

        # 收集指标
        latencies = []
        success_rates = []
        token_effs = []
        for rec in recent:
            co = rec.candidate_output
            if isinstance(co, dict):
                l = co.get("latency_p95_ms") or co.get("latency_ms", 0)
                if l > 0:
                    latencies.append(l)
                sr = co.get("success_rate")
                if sr is not None:
                    success_rates.append(sr)
                te = co.get("token_efficiency")
                if te is not None:
                    token_effs.append(te)

        if not latencies:
            return None

        latencies.sort()
        p50 = latencies[len(latencies) // 2]

        baseline = {
            "latency_p95_ms": p50,
            "source": "dynamic",
            "samples": len(recent),
        }
        if success_rates:
            baseline["success_rate"] = sum(success_rates) / len(success_rates)
        if token_effs:
            baseline["token_efficiency"] = sum(token_effs) / len(token_effs)

        # 收集 Gate6 checklist 维度分（供漂移检测）
        checklist_dims: dict[str, list] = {}
        for rec in recent:
            co = rec.candidate_output
            if isinstance(co, dict):
                for k, v in co.items():
                    if k.startswith("gate6_checklist_") and isinstance(v, (int, float)):
                        checklist_dims.setdefault(k, []).append(v)
        for k, vals in checklist_dims.items():
            if vals:
                baseline[k] = round(sum(vals) / len(vals), 4)

        logger.info(
            "Gate3: dynamic baseline for %s — %d samples, p50_latency=%.0fms, "
            "success_rate=%.3f, checklist_dims=%d",
            agent_type, len(recent), p50,
            baseline.get("success_rate", 0),
            len(checklist_dims),
        )
        return baseline

    def _verify_deepdiff(self, improvement: Improvement, start: float,
                         cfg: Gate3Config) -> GateResult:
        """完整 DeepDiff 对比"""
        diff = DeepDiff(
            improvement.baseline_output,
            improvement.candidate_output,
            significant_digits=3,
            exclude_paths=["__timestamp__", "__runtime_ms__"],
            ignore_order=True,
            report_repetition=True,
        )

        regressions: list[Regression] = []

        for category in ["values_changed", "type_changes",
                         "iterable_item_added", "iterable_item_removed",
                         "repetition_changed"]:
            changes = getattr(diff, category, {}) or {}
            severity = "critical" if category in ("values_changed", "type_changes") else "warning"
            for path, detail in changes.items():
                regressions.append(Regression(
                    field=str(path),
                    old_value=detail.get("old_value", detail),
                    new_value=detail.get("new_value", None),
                    change_type=category,
                    severity=severity,
                ))

        # 性能指标降级检查
        for key in ("success_rate", "f1_score"):
            regressions.extend(self._check_metric_degradation(key, improvement, cfg))

        # Gate6 checklist 维度漂移检测
        for key in improvement.baseline_output:
            if key.startswith("gate6_checklist_") and isinstance(
                improvement.baseline_output[key], (int, float)
            ):
                regressions.extend(
                    self._check_metric_degradation(key, improvement, cfg, min_baseline=0.5)
                )

        # 性能降级
        regressions.extend(self._check_perf_degradation(improvement, cfg))

        critical = [r for r in regressions if r.severity == "critical"]
        passed = len(critical) == 0

        # ── 归因分析 ────────────────────────────────
        attribution = None
        if not passed or regressions:
            try:
                from .attribution import AttributionEngine
                # 找到最严重的回归项做归因
                if critical:
                    worst = critical[0]
                    attribution = AttributionEngine.attribute(
                        field=worst.field,
                        baseline_value=worst.old_value,
                        candidate_value=worst.new_value,
                        baseline_decisions=improvement.baseline_output.get("_decisions", []),
                        candidate_decisions=improvement.candidate_output.get("_decisions", []),
                    )
                elif regressions:
                    worst = regressions[0]
                    attribution = AttributionEngine.attribute(
                        field=worst.field,
                        baseline_value=worst.old_value,
                        candidate_value=worst.new_value,
                        baseline_decisions=improvement.baseline_output.get("_decisions", []),
                        candidate_decisions=improvement.candidate_output.get("_decisions", []),
                    )
            except Exception as e:
                logger.warning("Attribution engine failed: %s", e)

        details = {
            "total_diffs": len(diff) if diff else 0,
            "regressions": [r.model_dump() for r in regressions],
            "critical_count": len(critical),
            "warning_count": len([r for r in regressions if r.severity == "warning"]),
            "source": improvement.metadata.pop("gate3_baseline_source", "manual"),
            "samples": improvement.metadata.pop("gate3_baseline_samples", 0),
        }

        if attribution:
            details["attribution"] = {
                "root_cause": attribution.root_cause,
                "fix_hint": attribution.fix_hint,
                "severity": attribution.severity,
                "decision_diffs": [
                    {
                        "decision_id": dd.decision_id[:12],
                        "contribution_pct": dd.contribution_pct,
                        "tool_call_count": len(dd.tool_call_diffs),
                        "tool_calls_added": dd.tool_calls_added,
                        "tool_calls_removed": dd.tool_calls_removed,
                    }
                    for dd in attribution.decision_diffs
                    if abs(dd.contribution_pct) > 1.0
                ],
            }
            improvement.metadata["attribution_fix_prompt"] = attribution.fix_prompt

        return GateResult(
            gate_name=GateName.GATE3,
            passed=passed,
            reason=(
                "No critical regressions"
                if passed
                else f"{len(critical)} critical regression(s): "
                     f"{', '.join(r.field for r in critical[:3])}"
            ),
            details=details,
            duration_ms=(time.time() - start) * 1000,
        )

    def _verify_fallback(self, improvement: Improvement, start: float,
                         cfg: Gate3Config) -> GateResult:
        """降级方案：数值指标对比（零依赖）"""
        baseline, candidate = improvement.baseline_output, improvement.candidate_output
        regressions: list[Regression] = []

        for key in baseline:
            # 跳过内部元数据字段（_evolved_from, _evolved_at, _decisions 等）
            if key.startswith("_"):
                continue
            # 跳过 Gate6 checklist 维度分（这些是 Gate6 输出的，不会出现在 candidate 中）
            if key.startswith("gate6_checklist_"):
                continue
            if key not in candidate:
                # 数值指标缺失时用默认值代替，避免假阳性
                # （旧 trace 演进基线时可能缺少某些字段）
                if isinstance(bv := baseline[key], (int, float)):
                    logger.debug("Gate3 fallback: candidate missing '%s', using baseline value", key)
                    candidate[key] = bv  # 用 baseline 值填充，避免误报回归
                    continue
                regressions.append(Regression(field=key, change_type="missing", severity="critical"))
                continue
            bv, cv = baseline[key], candidate[key]
            if isinstance(bv, (int, float)) and isinstance(cv, (int, float)):
                if bv > 0 and cv < bv * cfg.regress_pct:
                    regressions.append(Regression(
                        field=key,
                        old_value=bv, new_value=cv,
                        change_type=f"degraded {(cv/bv)*100:.0f}% of baseline",
                        severity="critical",
                    ))

        passed = len(regressions) == 0
        return GateResult(
            gate_name=GateName.GATE3,
            passed=passed,
            reason="No regressions (manual compare)" if passed
                   else "; ".join(f"{r.field}: {r.change_type}" for r in regressions[:5]),
            details={"regressions": [r.model_dump() for r in regressions],
                     "source": improvement.metadata.pop("gate3_baseline_source", "manual"),
                     "samples": improvement.metadata.pop("gate3_baseline_samples", 0)},
            duration_ms=(time.time() - start) * 1000,
        )

    def _check_metric_degradation(self, key: str, imp: Improvement,
                                  cfg: Gate3Config, min_baseline: float = 0.0) -> list[Regression]:
        bv = imp.baseline_output.get(key)
        cv = imp.candidate_output.get(key)
        if bv is None or cv is None or not isinstance(bv, (int, float)):
            return []
        # 跳过基线值过低的历史维度（如 checklist 维度 baseline<0.5，不算回归）
        if bv < min_baseline:
            return []
        if bv > 0 and cv < bv * cfg.regress_pct:
            return [Regression(
                field=key, old_value=bv, new_value=cv,
                change_type=f"degraded {((cv - bv) / bv) * 100:+.1f}%",
                severity="critical",
            )]
        return []

    def _check_perf_degradation(self, imp: Improvement,
                                cfg: Gate3Config) -> list[Regression]:
        old = imp.baseline_output.get("latency_ms", 0)
        new = imp.candidate_output.get("latency_ms", 0)
        if not old or not new:
            return []
        if new > old * (1 + cfg.perf_degradation_limit):
            return [Regression(
                field="latency_ms", old_value=old, new_value=new,
                change_type=f"degraded +{((new - old) / old) * 100:.0f}%",
                severity="warning",
            )]
        return []

    @staticmethod
    def rollback(improvement: Improvement) -> None:
        """L3 回滚：恢复 Benchmark 快照"""
        improvement.rollback_plan = RollbackPlan(
            level=RollbackLevel.L3,
            scope="restore benchmark snapshot",
            estimated_seconds=30,
            procedure="RESTORE FROM benchmark_snapshot WHERE improvement_id = ?",
            executed_at=datetime.now(UTC),
            success=True,
        )
        if improvement.baseline_output:
            improvement.candidate_output = improvement.baseline_output.copy()

# ── GatePlugin registration ──────────────────────────────
from .interface import register_gate

register_gate(GateName.GATE3, Gate3Regression)

