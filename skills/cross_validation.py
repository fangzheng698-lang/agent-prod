"""
Cross Validation Skill：独立交叉验证引擎
- 多维度质量评分
- 对抗性审查
- 自动重试与修复
"""

from __future__ import annotations
import asyncio
import json
import time
from typing import Any
from pydantic import BaseModel, Field

from app.llm import LLMClient, LLMResponse


# ── 评测维度 ──

class ReviewDimension(BaseModel):
    """单个审查维度"""
    name: str
    score: float = 0.0       # 0.0 - 1.0
    passed: bool = False
    issues: list[str] = Field(default_factory=list)
    suggestion: str = ""


class ReviewReport(BaseModel):
    """完整的审查报告"""
    task_id: str = ""
    approved: bool = False
    overall_score: float = 0.0
    dimensions: list[ReviewDimension] = Field(default_factory=list)
    summary: str = ""
    needs_human_review: bool = False


# ── 审查引擎 ──

class CrossValidationEngine:
    """
    交叉验证引擎。
    
    - 从多个独立维度评估输出质量
    - 可配置审查粒度
    - 支持批量和增量模式
    """

    DEFAULT_DIMENSIONS = [
        {
            "name": "factual_accuracy",
            "prompt": "检查事实准确性：结果中的每个断言是否都有依据？有无幻觉？",
        },
        {
            "name": "completeness",
            "prompt": "检查完整度：任务要求是否全部覆盖？有无遗漏？",
        },
        {
            "name": "reasoning_quality",
            "prompt": "检查推理质量：逻辑链是否完整？结论是否有充分支撑？",
        },
        {
            "name": "actionability",
            "prompt": "检查可操作性：结果是否具体、可执行？还是模糊笼统？",
        },
    ]

    def __init__(
        self,
        reviewer_llm: LLMClient,
        confidence_threshold: float = 0.7,
        dimensions: list[dict] | None = None,
    ):
        self._llm = reviewer_llm
        self._threshold = confidence_threshold
        self._dimensions = dimensions or self.DEFAULT_DIMENSIONS
        self._stats = {"reviews": 0, "approved": 0, "rejected": 0, "retries": 0}

    async def review(
        self,
        task_id: str,
        input_text: str,
        output_text: str,
        *,
        context: dict | None = None,
    ) -> ReviewReport:
        """
        对单次输出执行多维度审查。
        
        参数:
            task_id: 任务标识
            input_text: 原始输入（任务描述）
            output_text: 待审查的输出
            context: 额外上下文
        """
        self._stats["reviews"] += 1

        dimensions = []
        for dim in self._dimensions:
            result = await self._review_dimension(dim, input_text, output_text, context)
            dimensions.append(result)

        overall = sum(d.score for d in dimensions) / max(len(dimensions), 1)
        approved = overall >= self._threshold

        if approved:
            self._stats["approved"] += 1
        else:
            self._stats["rejected"] += 1

        return ReviewReport(
            task_id=task_id,
            approved=approved,
            overall_score=round(overall, 3),
            dimensions=dimensions,
            summary=self._generate_summary(dimensions, overall),
            needs_human_review=overall < 0.4,
        )

    async def _review_dimension(
        self,
        dim: dict,
        input_text: str,
        output_text: str,
        context: dict | None,
    ) -> ReviewDimension:
        """审查单个维度"""
        prompt = f"""请作为第三方审查员，评估以下维度：

审查维度: {dim['name']}
{dim['prompt']}

<原始任务>
{input_text[:1500]}
</原始任务>

<执行结果>
{output_text[:3000]}
</执行结果>

请以 JSON 格式返回:
{{
    "score": <0.0-1.0>,
    "issues": [<问题列表>],
    "suggestion": "<改进建议>"
}}"""

        resp = await self._llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )

        try:
            data = json.loads(resp.content or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {"score": 0.5, "issues": ["Failed to parse review"], "suggestion": ""}

        return ReviewDimension(
            name=dim["name"],
            score=min(max(float(data.get("score", 0)), 0.0), 1.0),
            passed=float(data.get("score", 0)) >= self._threshold,
            issues=data.get("issues", []),
            suggestion=data.get("suggestion", ""),
        )

    def _generate_summary(self, dimensions: list[ReviewDimension], overall: float) -> str:
        """生成审查摘要"""
        failed = [d for d in dimensions if not d.passed]
        if not failed:
            return "所有维度均通过审查 ✓"
        return f"通过率 {overall:.0%}。需改进维度: {', '.join(d.name for d in failed)}"

    async def batch_review(
        self,
        items: list[dict],
        *,
        max_concurrent: int = 5,
    ) -> list[ReviewReport]:
        """批量审查（并行执行，限流）"""
        sem = asyncio.Semaphore(max_concurrent)

        async def limited_review(item):
            async with sem:
                return await self.review(
                    item.get("task_id", "unknown"),
                    item.get("input", ""),
                    item.get("output", ""),
                    context=item.get("context"),
                )

        tasks = [limited_review(item) for item in items]
        return await asyncio.gather(*tasks)

    async def review_and_repair(
        self,
        task_id: str,
        input_text: str,
        output_fn,
        max_repair_attempts: int = 3,
    ) -> tuple[ReviewReport, str]:
        """
        审查 → 如果不通过 → 自动修复 → 再审查（循环）
        
        参数:
            task_id: 任务标识
            input_text: 任务描述
            output_fn: 重新生成的异步函数，接收 repair_instruction 返回新输出
            max_repair_attempts: 最大修复次数
        返回:
            (ReviewReport, 最终输出文本)
        """
        current_output = await output_fn("")

        for attempt in range(max_repair_attempts + 1):
            report = await self.review(task_id, input_text, current_output)

            if report.approved:
                return report, current_output

            if attempt >= max_repair_attempts:
                return report, current_output

            self._stats["retries"] += 1

            # 生成修复指令
            repair_instruction = self._build_repair_instruction(report)
            current_output = await output_fn(repair_instruction)

        return report, current_output

    def _build_repair_instruction(self, report: ReviewReport) -> str:
        """从审查报告生成修复指令"""
        issues = []
        for dim in report.dimensions:
            if not dim.passed:
                issues.append(f"  - [{dim.name}] {'; '.join(dim.issues[:2])}")
                if dim.suggestion:
                    issues.append(f"    → {dim.suggestion}")

        return f"【审查反馈】\n" + "\n".join(issues) + "\n\n请根据以上反馈修正输出。"

    def get_stats(self) -> dict:
        return dict(self._stats)
