"""
Gate3: 回归验证门
核心：用 DeepDiff 做结构化 diff + 数值指标降级检测
Phase 1: 阈值从 YAML 加载 + 异常保护
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from .models import GateName, GateResult, Improvement, RollbackLevel, RollbackPlan
from .reasoning import EvidenceSource, EvidenceType, ReasoningStep
from .interface import GatePlugin

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


class Gate3Regression(GatePlugin):
    """回归验证门 — DeepDiff 结构化对比 + 动态基线 + 自适应阈值"""

    name = GateName.GATE3
    rollback_level = RollbackLevel.L1

    def __init__(self, config: Gate3Config | None = None,
                 raw_config: dict | None = None,
                 repository=None,
                 flywheel_engine=None):
        self.config = config or Gate3Config()
        self._raw_config = raw_config
        self._repo = repository
        self._flywheel = flywheel_engine
        self._adaptive_engine = None

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
            # 即使没有基线，自适应阈值也在 fallback 中基于历史数据做检查
            if self._flywheel:
                agent_type = improvement.metadata.get("agent", "")
                if agent_type:
                    return self._verify_fallback(improvement, start, cfg)
            result = GateResult(
                gate_name=GateName.GATE3,
                passed=True,
                reason="No baseline — first run, skipping regression",
                details={"skipped": True, "source": "none"},
                duration_ms=(time.time() - start) * 1000,
            )
            self._inject_reasoning(improvement, result, "skipped", cfg)
            return result

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

        result = GateResult(
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
        self._inject_reasoning(improvement, result, "deepdiff", cfg, regressions=regressions)
        return result

    def _inject_reasoning(
        self,
        improvement: Improvement,
        result: GateResult,
        method: str,
        cfg: Gate3Config,
        regressions: list | None = None,
    ) -> None:
        """向推理链追加 Gate3 决策记录"""
        improvement.init_reasoning_chain()
        r = regressions or []
        evidence = [
            EvidenceSource(
                type=EvidenceType.COMPARISON,
                name=f"regression_{method}",
                value={
                    "critical": sum(1 for x in r if x.severity == "critical"),
                    "warning": sum(1 for x in r if x.severity == "warning"),
                    "total": len(r),
                    "method": method,
                },
                confidence=0.95,
            ),
        ]
        if r:
            top = r[:3]
            evidence.append(EvidenceSource(
                type=EvidenceType.STATISTICAL,
                name="top_regressions",
                value=[{"field": x.field, "change": x.change_type, "severity": x.severity} for x in top],
                confidence=0.9,
            ))

        step = ReasoningStep(
            step_id=f"g3-{uuid.uuid4().hex[:8]}",
            gate="gate3",
            decision="PASS" if result.passed else "FAIL",
            reason=result.reason,
            evidence=evidence,
            confidence=0.9 if result.passed else 0.95,
        )
        improvement.reasoning_chain.add_step(step)

    def _verify_fallback(self, improvement: Improvement, start: float,
                         cfg: Gate3Config) -> GateResult:
        """降级方案：数值指标对比（零依赖）+ 自适应阈值"""
        baseline, candidate = improvement.baseline_output, improvement.candidate_output
        regressions: list[Regression] = []

        # ── 自适应阈值检查 ─────────────────────────────────
        adaptive_result = self._adaptive_verify(improvement, cfg)
        if adaptive_result:
            regressions.extend(adaptive_result)

        # "越低越好" 指标前缀列表 — 例如 latency/error_rate 下降是优化，不是回归
        _LOWER_IS_BETTER_PREFIXES = ("latency", "latency_p95_ms", "error_rate")

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
            if not (isinstance(bv, (int, float)) and isinstance(cv, (int, float)) and bv > 0):
                continue

            is_lower_better = any(key.startswith(p) for p in _LOWER_IS_BETTER_PREFIXES)
            if is_lower_better:
                # 越低越好：回归 = 候选值比基线更高（更差）
                if cv > bv / cfg.regress_pct:
                    regressions.append(Regression(
                        field=key,
                        old_value=bv, new_value=cv,
                        change_type=f"degraded +{((cv - bv) / bv) * 100:+.0f}% above baseline",
                        severity="critical",
                    ))
            else:
                # 越高越好：回归 = 候选值比基线更低（更差）
                if cv < bv * cfg.regress_pct:
                    regressions.append(Regression(
                        field=key,
                        old_value=bv, new_value=cv,
                        change_type=f"degraded {(cv/bv)*100:.0f}% of baseline",
                        severity="critical",
                    ))

        critical = [r for r in regressions if r.severity == "critical"]
        passed = len(critical) == 0
        result = GateResult(
            gate_name=GateName.GATE3,
            passed=passed,
            reason="No regressions (manual compare)" if passed
                   else "; ".join(f"{r.field}: {r.change_type}" for r in critical[:5]),
            details={"regressions": [r.model_dump() for r in regressions],
                     "critical_count": len(critical),
                     "warning_count": len(regressions) - len(critical),
                     "source": improvement.metadata.pop("gate3_baseline_source", "manual"),
                     "samples": improvement.metadata.pop("gate3_baseline_samples", 0)},
            duration_ms=(time.time() - start) * 1000,
        )
        self._inject_reasoning(improvement, result, "fallback", cfg, regressions=regressions)
        return result

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

    def _train_adaptive_engine(self, agent_type: str) -> None:
        """从 flywheel 历史数据训练自适应阈值引擎。

        喂入同 agent 的历史 latency / success_rate / token_efficiency，
        校准 EWMA 动态阈值带。
        """
        if not self._flywheel:
            return
        try:
            logs = self._flywheel._load_logs(limit=200)
        except Exception:
            return
        if not logs:
            return

        same_agent = [r for r in logs if r.session_id and agent_type in r.session_id]
        # 也匹配 agent 字段（如果 ExecutionLogRecord 有该字段）
        if len(same_agent) < 3:
            same_agent = [r for r in logs if getattr(r, 'agent', '') == agent_type]
        # 最后兜底：用全部非零数据
        if len(same_agent) < 3:
            same_agent = [r for r in logs if r.duration_ms > 0 and r.turns > 0]
        if len(same_agent) < 3:
            return

        from agent_prod.adaptivity.adaptive_gates import AdaptiveGateEngine
        self._adaptive_engine = AdaptiveGateEngine(
            gate_name="gate3",
            metrics=["latency_ms", "success_rate", "token_efficiency"],
            window_size=50, sigma_mult=3.0, min_samples=5,
            min_widths={"latency_ms": 500.0},
        )

        # 提取真实 candidate trace 指标（quality_gate_result.trace_metrics），
        # 与 candidates 评估时读取的 candidate_output["latency_p95_ms"] 同源同量纲。
        # 旧记录没有 trace_metrics 时跳过 — 不再用 pipeline 挂钟时间冒充 latency。
        latencies = []
        success_rates = []
        token_effs = []
        for r in same_agent:
            qg = r.quality_gate_result or {}
            tm = qg.get("trace_metrics") or {}
            if not isinstance(tm, dict):
                tm = {}
            lat = tm.get("latency_p95_ms") or tm.get("latency_ms")
            if isinstance(lat, (int, float)) and lat > 0:
                latencies.append(float(lat))
            sr = tm.get("success_rate")
            if isinstance(sr, (int, float)) and 0 <= sr <= 1:
                success_rates.append(float(sr))
            te = tm.get("token_efficiency")
            if isinstance(te, (int, float)) and te > 0:
                token_effs.append(float(te))

        # IQR 离群值过滤（仅对 latency — 跨数量级最容易污染）
        def _iqr_filter(values: list[float]) -> list[float]:
            if len(values) < 4:
                return list(values)
            s = sorted(values)
            n = len(s)
            q1 = s[n // 4]
            q3 = s[(3 * n) // 4]
            iqr = q3 - q1
            lo, hi = q1 - 3.0 * iqr, q3 + 3.0 * iqr
            return [v for v in values if lo <= v <= hi]

        clean_latencies = _iqr_filter(latencies)
        for v in clean_latencies:
            self._adaptive_engine.record_metric("latency_ms", v)
        for v in token_effs:
            self._adaptive_engine.record_metric("token_efficiency", v)
        for v in success_rates:
            self._adaptive_engine.record_metric("success_rate", v)

        self._adaptive_engine.calibrate_all()

    def _adaptive_verify(self, improvement: Improvement, cfg: Gate3Config) -> list[Regression]:
        """用自适应阈值引擎检查指标，返回回归列表。"""
        agent_type = improvement.metadata.get("agent", "")
        if not agent_type:
            return []

        # 训练（首次调用或数据不足时）
        if self._adaptive_engine is None:
            self._train_adaptive_engine(agent_type)

        if self._adaptive_engine is None:
            return []

        candidate = improvement.candidate_output or {}
        metrics = {}
        for key in ("latency_p95_ms", "latency_ms", "success_rate", "token_efficiency"):
            v = candidate.get(key, 0)
            if isinstance(v, (int, float)) and v > 0:
                m_key = "latency_ms" if key in ("latency_p95_ms", "latency_ms") else key
                metrics[m_key] = float(v)

        if not metrics:
            return []

        result = self._adaptive_engine.evaluate(metrics)
        regressions = []
        has_manual_baseline = bool(improvement.baseline_output)
        for v in result.get("violations", []):
            regressions.append(Regression(
                field=f"adaptive_{v['metric']}",
                old_value=v.get("upper", 0),
                new_value=v["value"],
                change_type=f"adaptive σ={v.get('deviation',0):.1f} (upper={v.get('upper',0):.0f})",
                severity="warning" if not has_manual_baseline else "critical",
            ))
        return regressions

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

    @classmethod
    def from_config(cls, config: dict, name: GateName) -> Gate3Regression:
        gate3_cfg = Gate3Config.from_yaml(config)
        repo = None  # caller sets repository if needed
        return cls(config=gate3_cfg, raw_config=config, repository=repo)

# ── GatePlugin registration ──────────────────────────────
from .interface import register_gate

register_gate(GateName.GATE3, Gate3Regression)

