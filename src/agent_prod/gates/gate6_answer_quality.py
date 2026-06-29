"""Gate6: 答案正确性评估门 — LLM-as-judge / 精确匹配 / 语义相似度。

独立于 Gate3（性能回归），专门评估 agent 输出的答案是否满足质量要求。
支持三种评估器，通过 config.yaml gate6.evaluator 切换：
  - llm-judge: LLM 对比候选回答与期望答案，输出 0-1 分
  - exact-match: 字符串精确匹配
  - semantic: 基于嵌入的语义相似度（可选依赖）

config.yaml 示例:
  gate6:
    enabled: true
    evaluator: llm-judge
    pass_threshold: 0.7        # 低于此分 REJECT
    timeout_seconds: 30.0
    fallback_on_timeout: pass  # pass | reject | skip
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib import request

from .models import GateName, GateResult, Improvement

logger = logging.getLogger(__name__)


@dataclass
class Gate6Config:
    """Gate6 评估配置"""

    enabled: bool = True
    evaluator: str = "checklist"  # checklist | llm-judge | exact-match | semantic | mock
    pass_threshold: float = 0.70
    timeout_seconds: float = 30.0
    fallback_on_timeout: str = "pass"   # pass | reject | skip
    llm_model: str = ""     # 空则用 OPENAI_MODEL
    llm_endpoint: str = ""
    llm_api_key_env: str = "OPENAI_API_KEY"
    llm_api_key: str = ""   # 空则用 OPENAI_API_KEY

    def _auto_detect_llm(self) -> None:
        """Auto-detect LLM config from environment if not explicitly set."""
        import os
        if not self.llm_model:
            self.llm_model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        if not self.llm_endpoint:
            self.llm_endpoint = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not os.environ.get(self.llm_api_key_env):
            self.llm_api_key_env = "OPENAI_API_KEY"  # fallback to standard

    @classmethod
    def from_yaml(cls, raw: dict | None) -> Gate6Config:
        if not raw:
            return cls()
        g6 = raw.get("gates", {}).get("gate6", {})
        if not g6:
            return cls()

        enabled = g6.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.lower() not in ("false", "0", "no", "off")
        else:
            enabled = bool(enabled)

        return cls(
            enabled=enabled,
            evaluator=g6.get("evaluator", "llm-judge"),
            pass_threshold=float(g6.get("pass_threshold", 0.70)),
            timeout_seconds=float(g6.get("timeout_seconds", 30.0)),
            fallback_on_timeout=g6.get("fallback_on_timeout", "pass"),
            llm_endpoint=g6.get("llm_endpoint", ""),
            llm_model=g6.get("llm_model", ""),
            llm_api_key=g6.get("llm_api_key", ""),
        )


class Gate6AnswerQuality:
    """答案正确性质量门。

    评估 agent 输出与期望答案之间的吻合度。
    支持三种评估器，通过 Gate6Config 配置。
    """

    def __init__(self, config: Gate6Config | None = None):
        self.config = config or Gate6Config()
        self._last_candidate = ""
        self._last_expected = ""

    def rollback(self, improvement: Improvement) -> None:
        """Gate6 回滚策略：无副作用，无需回滚。
        
        Gate6 仅评估答案质量，不产生副作用。
        如果答案质量不达标，拒绝上线即可，无需清理操作。
        """
        pass

    def verify(self, improvement: Improvement) -> GateResult:
        start = time.time()
        cfg = self.config

        if not cfg.enabled:
            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason="Gate6 disabled in config — skipping answer quality check",
                details={"skipped": True, "reason": "disabled"},
                duration_ms=(time.time() - start) * 1000,
            )

        # ── Watchdog 监控会话：多轮对话不适合做单条 Q&A 质量评估 ──
        source = improvement.metadata.get("source", "")
        if "watchdog" in source or "monitor" in source:
            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason=f"Watchdog/monitoring session ({source}) — skipping answer quality (multi-turn not Q&A)",
                details={"skipped": True, "reason": "watchdog_session"},
                duration_ms=(time.time() - start) * 1000,
            )

        # 判断是否有可评估的数据
        candidate = improvement.candidate_output.get("final_response", "")
        expected = improvement.candidate_output.get("expected_answer", "")
        user_q = improvement.candidate_output.get("user_question", "")
        self._last_candidate = candidate
        self._last_expected = expected

        if not candidate and not expected:
            # 检查是否有预填充的分数
            pre_scored = candidate_fields_to_score(improvement.candidate_output)
            if pre_scored:
                return self._evaluate_pre_scored(pre_scored, cfg, start)

            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason="No answer quality data — skipping (add expected_answer or f1_score/accuracy to candidate_output)",
                details={"skipped": True, "reason": "no_data"},
                duration_ms=(time.time() - start) * 1000,
            )

        # ── no-ref-llm: 无参考评估，只需 candidate + user_question ──
        if cfg.evaluator == "no-ref-llm":
            if candidate and user_q:
                return self._evaluate_no_ref_llm(candidate, user_q, improvement, cfg, start)
            if candidate:
                return self._evaluate_no_ref_llm(candidate, "", improvement, cfg, start)
            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason="no-ref-llm evaluator: no candidate response to evaluate",
                details={"skipped": True, "reason": "no_candidate"},
                duration_ms=(time.time() - start) * 1000,
            )

        # ── checklist: 二值清单评估，算法稳定无需调 prompt ──
        if cfg.evaluator == "checklist":
            if candidate and user_q:
                return self._evaluate_checklist(candidate, user_q, improvement, cfg, start)
            if candidate:
                return self._evaluate_checklist(candidate, "", improvement, cfg, start)
            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason="checklist evaluator: no candidate response to evaluate",
                details={"skipped": True, "reason": "no_candidate"},
                duration_ms=(time.time() - start) * 1000,
            )

        # ── 有 expected_answer 时走配对评估 ──
        if expected:
            if cfg.evaluator == "exact-match":
                return self._evaluate_exact(candidate, expected, start)
            elif cfg.evaluator == "llm-judge":
                return self._evaluate_llm_judge(candidate, expected, improvement, cfg, start)
            elif cfg.evaluator == "semantic":
                return self._evaluate_semantic(candidate, expected, start)
            elif cfg.evaluator == "mock":
                return self._evaluate_mock(improvement, cfg, start)

        # ── 无 expected_answer，降级为 checklist（比 no-ref-llm 更稳定）──
        if candidate and user_q:
            return self._evaluate_checklist(candidate, user_q, improvement, cfg, start)

        return GateResult(
            gate_name=GateName.GATE6,
            passed=True,
            reason="No expected answer provided — cannot evaluate correctness",
            details={"skipped": True, "reason": "no_expected"},
            duration_ms=(time.time() - start) * 1000,
        )

    # ── 评估器实现 ──────────────────────────────────────────

    def _evaluate_exact(self, candidate: str, expected: str, start: float) -> GateResult:
        score = 1.0 if candidate.strip() == expected.strip() else 0.0
        return self._make_result(score, start, {"method": "exact-match"})

    def _evaluate_llm_judge(
        self, candidate: str, expected: str,
        improvement: Improvement, cfg: Gate6Config, start: float,
    ) -> GateResult:
        """LLM-as-judge: 用 LLM 对比候选回答与期望答案。"""
        api_key = (cfg.llm_api_key
                   or os.environ.get("OPENAI_API_KEY", "")
                   or os.environ.get("GATE6_LLM_API_KEY", ""))
        base_url = (cfg.llm_endpoint
                    or os.environ.get("OPENAI_BASE_URL", "")
                    or os.environ.get("GATE6_LLM_BASE_URL", ""))
        model = (cfg.llm_model
                 or os.environ.get("OPENAI_MODEL", "")
                 or os.environ.get("GATE6_LLM_MODEL", ""))

        if not api_key or not base_url:
            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason="LLM judge not configured — set OPENAI_API_KEY/OPENAI_BASE_URL or gate6.llm_*",
                details={"skipped": True, "reason": "llm_not_configured"},
                duration_ms=(time.time() - start) * 1000,
            )

        prompt = _build_judge_prompt(candidate, expected)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are an answer quality evaluator. Output only JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 800,
        }

        try:
            url = f"{base_url.rstrip('/')}/chat/completions"
            req = request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with request.urlopen(req, timeout=cfg.timeout_seconds) as resp:
                body = json.loads(resp.read())
                content = body["choices"][0]["message"]["content"]
                # Parse JSON — fault-tolerant
                result = _parse_judge_response(content)
                score = float(result.get("score", 0.5))
                explanation = result.get("explanation", "")

                details = {
                    "method": "llm-judge",
                    "model": model,
                    "raw_score": score,
                    "explanation": explanation[:200],
                    "candidate_len": len(candidate),
                    "expected_len": len(expected),
                }
                return self._make_result(score, start, details)

        except Exception as e:
            logger.warning(f"Gate6 LLM judge failed: {e}")
            if cfg.fallback_on_timeout == "skip":
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=True,
                    reason=f"LLM judge failed ({e}), fallback=skip — passing",
                    details={"skipped": True, "reason": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )
            elif cfg.fallback_on_timeout == "reject":
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=False,
                    reason=f"LLM judge unavailable: {e}",
                    details={"error": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )
            else:  # pass
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=True,
                    reason=f"LLM judge failed ({e}), fallback=pass — passing",
                    details={"skipped": True, "reason": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )

    # ── No-Reference LLM Judge evaluator ────────────────────────
    def _evaluate_no_ref_llm(
        self, candidate: str, user_question: str,
        improvement: Improvement, cfg: Gate6Config, start: float,
    ) -> GateResult:
        """无参考 LLM-judge: 不需 expected_answer，直接评估回答质量。

        评估维度: relevance, completeness, coherence, accuracy, helpfulness.
        """
        api_key = (cfg.llm_api_key
                   or os.environ.get("OPENAI_API_KEY", "")
                   or os.environ.get("GATE6_LLM_API_KEY", ""))
        base_url = (cfg.llm_endpoint
                    or os.environ.get("OPENAI_BASE_URL", "")
                    or os.environ.get("GATE6_LLM_BASE_URL", ""))
        model = (cfg.llm_model
                 or os.environ.get("OPENAI_MODEL", "")
                 or os.environ.get("GATE6_LLM_MODEL", ""))

        if not api_key or not base_url:
            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason="no-ref-llm: LLM judge not configured — set OPENAI_API_KEY/OPENAI_BASE_URL",
                details={"skipped": True, "reason": "llm_not_configured"},
                duration_ms=(time.time() - start) * 1000,
            )

        prompt = _build_no_ref_judge_prompt(candidate, user_question)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are an AI answer quality evaluator. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 200,  # lite/qwen-flash 非推理，200 tokens 足够输出 JSON
        }

        try:
            url = f"{base_url.rstrip('/')}/chat/completions"
            req = request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with request.urlopen(req, timeout=cfg.timeout_seconds) as resp:
                body = json.loads(resp.read())
                content = body["choices"][0]["message"]["content"]
                result = _parse_judge_response(content)
                score = float(result.get("score", 0.5))
                explanation = result.get("explanation", "")

                # ── 用户反馈信号混合 ──────────────────────────
                # 检测用户在对话中的纠错/不满信号，影响最终评分
                user_sat = improvement.candidate_output.get("user_satisfaction", None)
                if user_sat is not None and isinstance(user_sat, (int, float)):
                    user_sat = float(user_sat)
                    corr_cnt = improvement.candidate_output.get("correction_count", 0)
                    blended = score * 0.7 + user_sat * 0.3  # LLM评分 70% + 用户反馈 30%
                    logger.info(
                        "Gate6 blended: llm=%.3f sat=%.2f corr=%d -> blended=%.3f (%.3f) [%s]",
                        score, user_sat, corr_cnt, blended,
                        improvement.candidate_output.get("relevance", 0),
                        improvement.id,
                    )
                else:
                    blended = score  # 无反馓信号时不混合

                details = {
                    "method": "no-ref-llm",
                    "model": model,
                    "score": round(blended, 4),
                    "explanation": explanation[:300],
                    "candidate_len": len(candidate),
                    "user_question_len": len(user_question),
                    "dimensions": {
                        k: result.get(k, None)
                        for k in ("relevance", "completeness", "coherence",
                                   "accuracy", "helpfulness")
                        if k in result
                    },
                }
                return self._make_result(blended, start, details)

        except Exception as e:
            logger.warning(f"Gate6 no-ref-llm failed: {e}")
            if cfg.fallback_on_timeout == "skip":
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=True,
                    reason=f"no-ref-llm failed ({e}), fallback=skip — passing",
                    details={"skipped": True, "reason": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )
            elif cfg.fallback_on_timeout == "reject":
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=False,
                    reason=f"no-ref-llm unavailable: {e}",
                    details={"error": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )
            else:
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=True,
                    reason=f"no-ref-llm failed ({e}), fallback=pass — passing",
                    details={"skipped": True, "reason": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )

    # ── Checklist evaluator (二值清单，算法稳定无需调 prompt) ──────

    def _evaluate_checklist(
        self, candidate: str, user_question: str,
        improvement: Improvement, cfg: Gate6Config, start: float,
    ) -> GateResult:
        """二值清单评估：LLM 对 10 个通用质量检查项做 y/n 判断。

        得分 = 通过数 / 10。不依赖 LLM 主观 0-1 打分，仅做二值判断，
        因此 prompt 措辞变化对结果影响极小，跨 agent/领域/模型均稳定。

        Checklist 项面向通用回答质量，不绑定特定 agent 或领域。
        """
        api_key = (cfg.llm_api_key
                   or os.environ.get("OPENAI_API_KEY", "")
                   or os.environ.get("GATE6_LLM_API_KEY", ""))
        base_url = (cfg.llm_endpoint
                    or os.environ.get("OPENAI_BASE_URL", "")
                    or os.environ.get("GATE6_LLM_BASE_URL", ""))
        model = (cfg.llm_model
                 or os.environ.get("OPENAI_MODEL", "")
                 or os.environ.get("GATE6_LLM_MODEL", ""))

        if not api_key or not base_url:
            return GateResult(
                gate_name=GateName.GATE6,
                passed=True,
                reason="checklist: LLM not configured — set OPENAI_API_KEY/OPENAI_BASE_URL",
                details={"skipped": True, "reason": "llm_not_configured"},
                duration_ms=(time.time() - start) * 1000,
            )

        prompt = _build_checklist_prompt(candidate, user_question,
            expected_plan=improvement.candidate_output.get("expected_plan", ""))
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "You are a quality checklist auditor. For each item, answer ONLY "
                    "yes or no. No explanations in the check fields. Be strict: any doubt "
                    "→ no. Output ONLY valid JSON."
                )},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 300,
        }

        try:
            url = f"{base_url.rstrip('/')}/chat/completions"
            req = request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with request.urlopen(req, timeout=cfg.timeout_seconds) as resp:
                body = json.loads(resp.read())
                content = body["choices"][0]["message"]["content"]
                result = _parse_judge_response(content)
                explanation = result.get("explanation", "")

                # 计数 yes — 动态调整分母（当没有 expected_plan 时排除 follows_plan）
                expected_plan = improvement.candidate_output.get("expected_plan", "")
                has_plan = bool(expected_plan and expected_plan.strip())
                effective_items = [k for k in _CHECKLIST_ITEMS if has_plan or k != "follows_plan"]

                passes = 0
                checks = {}
                for key in effective_items:
                    val = str(result.get(key, "no")).strip().lower()
                    is_yes = val in ("yes", "y", "true", "1", "pass")
                    checks[key] = is_yes
                    if is_yes:
                        passes += 1

                total_items = len(effective_items)
                score = round(passes / total_items, 2) if total_items > 0 else 1.0

                # 用户反馈混合
                user_sat = improvement.candidate_output.get("user_satisfaction", None)
                if user_sat is not None and isinstance(user_sat, (int, float)):
                    blended = score * 0.7 + float(user_sat) * 0.3
                    logger.info(
                        "Gate6 checklist blended: raw=%.2f sat=%.2f → blended=%.3f [%s]",
                        score, user_sat, blended, improvement.id,
                    )
                else:
                    blended = score

                details = {
                    "method": "checklist",
                    "model": model,
                    "score": round(blended, 4),
                    "raw_score": score,
                    "checks": checks,
                    "passes": passes,
                    "total_items": total_items,
                    "explanation": explanation[:300],
                    "candidate_len": len(candidate),
                    "user_question_len": len(user_question),
                }
                # ── 注入检查项维度分到 candidate_output，供 Gate3 漂移检测 ──
                for dim_name in effective_items:
                    improvement.candidate_output[f"gate6_checklist_{dim_name}"] = (
                        1 if checks[dim_name] else 0
                    )
                improvement.candidate_output["gate6_checklist_score"] = score

                # ── ErrorClassifier 修复：不覆盖 _last_expected（已在 verify() 中设置）──
                # _last_candidate 按 checklist 传入的 candidate 更新
                self._last_candidate = candidate

                # ── 生成 checklist 维度修复建议 ──
                failed_dims = [d for d in _CHECKLIST_ITEMS if not checks[d]]
                if failed_dims:
                    dim_hints = _checklist_fix_hints()
                    fix_lines = []
                    for d in failed_dims:
                        hint = dim_hints.get(d, "")
                        if hint:
                            fix_lines.append(f"- **{d}**: {hint}")
                    if fix_lines:
                        details["fix_suggestion"] = (
                            "Checklist 维度未通过:\n" + "\n".join(fix_lines)
                        )

                return self._make_result(blended, start, details)

        except Exception as e:
            logger.warning(f"Gate6 checklist failed: {e}")
            if cfg.fallback_on_timeout == "skip":
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=True,
                    reason=f"checklist failed ({e}), fallback=skip",
                    details={"skipped": True, "reason": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )
            elif cfg.fallback_on_timeout == "reject":
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=False,
                    reason=f"checklist unavailable: {e}",
                    details={"error": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )
            else:
                return GateResult(
                    gate_name=GateName.GATE6,
                    passed=True,
                    reason=f"checklist failed ({e}), fallback=pass",
                    details={"skipped": True, "reason": str(e)[:100]},
                    duration_ms=(time.time() - start) * 1000,
                )

    # ── Semantic evaluator ─────────────────────────────────────
    def _evaluate_semantic(self, candidate: str, expected: str,
                           start: float) -> GateResult:
        """本地语义相似度评估 — 零外部依赖，使用 Jaccard token 重叠。"""
        def _jaccard(a: str, b: str) -> float:
            set_a = set(a.lower().split())
            set_b = set(b.lower().split())
            if not set_a and not set_b:
                return 1.0
            if not set_a or not set_b:
                return 0.0
            intersection = len(set_a & set_b)
            union = len(set_a | set_b)
            return intersection / union if union > 0 else 0.0

        score = _jaccard(candidate, expected)
        details = {
            "score": round(score, 4),
            "evaluator": "semantic-jaccard",
            "candidate_len": len(candidate),
            "expected_len": len(expected),
        }
        return self._make_result(score, start, details)

    def _evaluate_mock(self, improvement: Improvement, cfg, start: float) -> GateResult:
        """Mock evaluator for testing — uses pre-scored f1_score/accuracy from candidate_output."""
        pre = candidate_fields_to_score(improvement.candidate_output)
        if pre:
            return self._evaluate_pre_scored(pre, cfg, start)
        # Fallback: always pass
        return self._make_result(0.85, start, {"method": "mock"})

    def _evaluate_pre_scored(self, scores: dict, cfg: Gate6Config, start: float) -> GateResult:
        """使用外部预填充的分数。"""
        f1 = scores.get("f1_score", 0.0)
        acc = scores.get("accuracy", 0.0)
        bleu = scores.get("bleu", 0.0)
        rouge = scores.get("rouge_l", 0.0)

        # 取已填充指标的平均分
        vals = [v for v in (f1, acc, bleu, rouge) if v > 0]
        avg_score = sum(vals) / len(vals) if vals else 0.5

        return self._make_result(avg_score, start, {
            "method": "pre-scored",
            "f1_score": f1,
            "accuracy": acc,
            "bleu": bleu,
            "rouge_l": rouge,
            "composite": round(avg_score, 4),
        })

    def _make_result(self, score: float, start: float, details: dict | None = None) -> GateResult:
        passed = score >= self.config.pass_threshold
        details = details or {}
        details["score"] = round(score, 4)
        details["threshold"] = self.config.pass_threshold

        # ── 错误分类 ─────────────────────────────
        if not passed:
            try:
                from .error_classifier import ErrorClassifier
                classification = ErrorClassifier.classify(
                    candidate=str(self._last_candidate)[:2000],
                    expected=str(self._last_expected)[:2000],
                    score=score,
                )
                details["error_class"] = classification.error_class.value
                details["error_confidence"] = round(classification.confidence, 2)
                details["error_reason"] = classification.reason
                details["error_evidence"] = classification.evidence
                details["fix_suggestion"] = classification.fix_suggestion
            except Exception as e:
                logger.warning("Gate6 error classifier failed: %s", e)

        return GateResult(
            gate_name=GateName.GATE6,
            passed=passed,
            reason=(
                f"Answer quality score {score:.3f} >= {self.config.pass_threshold}"
                if passed
                else f"Answer quality score {score:.3f} < {self.config.pass_threshold} — REJECTED"
            ),
            details=details,
            duration_ms=(time.time() - start) * 1000,
        )


# ── 工具函数 ──────────────────────────────────────────────
#
# ── Checklist evaluator: 通用回答质量检查项 (跨 agent/领域/模型稳定) ──

_CHECKLIST_ITEMS = (
    "addresses_question",     # 回答是否直接针对用户问题
    "is_substantial",         # 回答是否有实质内容 (非空泛/敷衍/仅重述问题)
    "attempts_answer",        # 是否真正尝试回答问题 (非回避/转移话题/仅给链接)
    "actionable",             # 是否提供可操作的具体信息 (非空泛理论)
    "no_hallucination",       # 无明显的编造、幻觉或与共识事实矛盾的内容
    "internally_consistent",  # 内部逻辑一致，无前后矛盾
    "covers_all_parts",       # 多问句是否逐一回应
    "well_structured",        # 结构清晰，层次分明，易读
    "concise",                # 简洁，无不必要的冗长/重复
    "enables_action",         # 用户能否据此直接采取行动
    "code_correct",           # 如有代码片段，是否正确可运行；无代码则自动通过
    "appropriate_tone",       # 语气恰当，专业得体
    "follows_plan",           # 是否按分配的计划执行，没有偏离或幻觉
)


def candidate_fields_to_score(candidate: dict) -> dict:
    """从 candidate_output 中提取预填充的评分字段。"""
    scores = {}
    for key in ("f1_score", "accuracy", "bleu", "rouge_l"):
        if key in candidate and isinstance(candidate[key], (int, float)):
            scores[key] = float(candidate[key])
    return scores


def _build_judge_prompt(candidate: str, expected: str) -> str:
    # 截断超长文本
    candidate_snip = candidate[:2000] if len(candidate) > 2000 else candidate
    expected_snip = expected[:2000] if len(expected) > 2000 else expected

    return f"""Evaluate the candidate answer against the expected answer.
Score from 0.0 (completely wrong) to 1.0 (perfect match).

Expected answer:
{expected_snip}

Candidate answer:
{candidate_snip}

Output ONLY valid JSON with "score" (float 0-1) and "explanation" (short string):
{{"score": 0.85, "explanation": "..."}}"""


def _build_no_ref_judge_prompt(candidate: str, user_question: str) -> str:
    """无参考评估 prompt — 只根据用户问题和回答本身评估质量。"""
    candidate_snip = candidate[:3000] if len(candidate) > 3000 else candidate
    question_snip = user_question[:1500] if len(user_question) > 1500 else user_question

    q_block = f"User's question:\n{question_snip}\n\n" if question_snip.strip() else ""
    return f"""Evaluate this AI assistant response WITHOUT a reference answer.
Score from 0.0 (terrible) to 1.0 (perfect).

{q_block}Assistant's response:
{candidate_snip}

Rate on 5 dimensions (each 0.0-1.0):
1. relevance: Does it directly address what was asked? (0=off-topic, 1=perfectly on-point)
2. completeness: Does it cover all parts of the question? (0=misses everything, 1=fully complete)
3. coherence: Is it well-structured and logical? (0=gibberish, 1=crystal clear)
4. accuracy: Are there factual errors, hallucinations, or contradictions? (0=full of errors, 1=factually sound)
5. helpfulness: Would this genuinely help the user? (0=useless, 1=extremely useful)

Output ONLY valid JSON:
{{"score": 0.85, "relevance": 0.9, "completeness": 0.8, "coherence": 0.9, "accuracy": 0.9, "helpfulness": 0.8, "explanation": "Brief overall assessment"}}"""


def _build_checklist_prompt(candidate: str, user_question: str, expected_plan: str = "") -> str:
    """清单评估 prompt — 要求 LLM 对每个检查项仅回答 yes/no。

    关键设计：不要求评分 0-1，仅做二值判断。因此 prompt 措辞变化不
    影响计分粒度，跨 agent/领域/模型迁移时无需调参。

    当提供 expected_plan 时，增加 follows_plan 维度判断是否按计划执行。
    """
    candidate_snip = candidate[:3000] if len(candidate) > 3000 else candidate
    question_snip = user_question[:1500] if len(user_question) > 1500 else user_question

    q_block = f"User's question:\n{question_snip}\n\n" if question_snip.strip() else ""

    # 决定是否包含 follows_plan 项
    has_plan = bool(expected_plan.strip())
    selected_items = _CHECKLIST_ITEMS
    if not has_plan and "follows_plan" in selected_items:
        # 当没有计划时排除 follows_plan（tuple -> list -> filter -> tuple）
        selected_items = tuple(item for item in selected_items if item != "follows_plan")

    plan_block = f"Assigned plan:\n{expected_plan[:1000]}\n\n" if has_plan else ""

    item_descriptions = (
        "Does the response directly address the user's question? (yes=on-topic, no=off-topic)",
        "Does the response have real, substantial content? (yes=meaningful answer, no=empty/placeholder/dismissive)",
        "Does it genuinely ATTEMPT to answer? (yes=effort made, no=dodges/deflects/just links)",
        "Does it provide actionable, specific information? (yes=concrete steps/facts, no=vagueness/generalities)",
        "Does it contain any obvious hallucinations, fabrications, or contradictions with well-known facts? (yes=clean, no=has false/imagined content)",
        "Is it internally consistent with no contradictions across paragraphs? (yes=consistent, no=self-contradictory)",
        "Does it cover all distinct parts of a multi-part question? (yes=all covered, no=some parts missed)",
        "Is it well-structured, easy to follow with clear organization? (yes=clear structure, no=messy/disorganized)",
        "Is it concise without unnecessary repetition or verbosity? (yes=concise, no=repetitive/verbose)",
        "Can the user take action based on this response alone? (yes=actionable, no=still needs clarification)",
        "If code is present, is it correct and runnable? If no code, answer yes. (yes=correct or no code, no=buggy/won't run)",
        "Is the tone appropriate and professional? (yes=proper, no=rude/dismissive/inappropriate)",
        "Does the response follow the assigned plan — doing ONLY what was planned, not deviating to unrelated tasks? (yes=follows plan, no=off-plan or hallucinated)",
    )
    # 当没有计划时，使用前 12 个描述（排除 follows_plan）
    active_descriptions = item_descriptions[:len(selected_items)]
    items_text = "\n".join(
        f"{i+1}. {name} — {desc}"
        for i, (name, desc) in enumerate(zip(selected_items, active_descriptions))
    )

    # JSON 示例也一样
    json_keys = ", ".join(f'"{item}": "yes"' for item in selected_items)

    return f"""Perform a binary quality checklist audit on this AI response.
Answer ONLY "yes" or "no" for each item. Be strict: any doubt -> no.

{q_block}{plan_block}Assistant's response:
{candidate_snip}

Checklist:
{items_text}

Output ONLY valid JSON (no markdown, no code fences):
{{{json_keys}, "explanation": "Brief overall assessment"}}"""


def _parse_judge_response(content: str) -> dict:
    """从 LLM 回复中提取 JSON。容错：处理 markdown 代码块和一些格式问题。"""
    # 尝试直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 代码块
    if "```" in content:
        try:
            start = content.index("```") + 3
            end = content.index("```", start)
            json_str = content[start:end].strip()
            if json_str.startswith("json"):
                json_str = json_str[4:].strip()
            return json.loads(json_str)
        except (ValueError, json.JSONDecodeError):
            pass

    # 尝试提取 { ... }
    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        return json.loads(content[start:end])
    except (ValueError, json.JSONDecodeError):
        pass

    return {"score": 0.5, "explanation": f"Parse error: {content[:100]}"}


def _checklist_fix_hints() -> dict[str, str]:
    """Checklist 维度修复建议映射 — 每项给出可操作的改进方向."""
    return {
        "addresses_question": "检查 prompt 是否清晰传达了用户意图，确保回答直接回应问题而不是跑题",
        "is_substantial": "回答内容过于空泛或仅重述问题。增加具体的分析、数据、或步骤",
        "attempts_answer": "回答回避了问题或只给出链接。确保真正尝试解答，而非转移话题",
        "actionable": "将抽象理论转化为可执行的具体步骤、命令、或操作指南",
        "no_hallucination": "核对输出中的事实声明，删除没有可靠来源的编造内容。降低 temperature 有帮助",
        "internally_consistent": "检查前后段落是否有矛盾。先确定核心结论，再围绕它组织论据",
        "covers_all_parts": "多问句场景需要逐一回应每个子问题。在回答中明确标注 '关于X...' '关于Y...'",
        "well_structured": "使用标题、编号、分段来组织内容。先总述再展开，最后总结",
        "concise": "删除重复表述和无关内容。每个段落表达一个核心观点",
        "enables_action": "确保用户读完就能动手。提供命令、路径、步骤，而非泛泛建议",
        "code_correct": "代码片段需可运行。提供完整的 import 和上下文，避免伪代码",
        "appropriate_tone": "检查语气是否专业、尊重。避免居高临下或不耐烦的表述",
    }
