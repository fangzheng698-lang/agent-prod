"""
Phase 5.2: Threshold Heatmap — 阈值热力图

在不同阈值组合下测试门禁通过率，生成 ASCII 热力图。
用途：可视化 token_limit × time_limit 对 pass_rate 的影响。

用法:
    grid = await run_heatmap(evaluator=my_fn, token_limits=[...], time_limits_ms=[...])
    print(grid.render_ascii())
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


EvaluatorFn = Callable[
    [dict[str, int]],
    Awaitable[tuple[bool, list[Any]]],
]
"""evaluator(thresholds) -> (passed, gate_results)"""


@dataclass
class ThresholdConfig:
    """阈值扫范围配置"""

    token_limits: list[int] = field(default_factory=lambda: [1000, 5000, 10000, 50000, 100000])
    time_limits_ms: list[int] = field(default_factory=lambda: [1000, 5000, 10000, 30000, 60000])
    confidence_threshold: float = 0.8


class HeatmapGrid:
    """
    2D 热力图网格。

    x_axis: token_limit 值
    y_axis: time_limit_ms 值
    数据存储为 pass_rate (0.0-1.0)
    """

    def __init__(
        self,
        x_axis: list[int],
        y_axis: list[int],
        x_label: str = "token_limit",
        y_label: str = "time_limit_ms",
    ):
        self.x_axis = list(x_axis)
        self.y_axis = list(y_axis)
        self.x_label = x_label
        self.y_label = y_label
        # grid[y][x] = pass_rate
        self._data: dict[tuple[int, int], float] = {}

    def set(self, xi: int, yi: int, value: float) -> None:
        """设置坐标 (xi, yi) 的通过率。xi 是 x_axis 索引，yi 是 y_axis 索引。"""
        if 0 <= xi < len(self.x_axis) and 0 <= yi < len(self.y_axis):
            self._data[(xi, yi)] = value

    def get(self, xi: int, yi: int) -> float | None:
        """获取坐标 (xi, yi) 的通过率。"""
        return self._data.get((xi, yi))

    def render_ascii(self, max_col_width: int = 8) -> str:
        """
        渲染纯文本 ASCII 热力图。

        每个单元格显示 pass_rate，使用颜色梯度符号表示。
        """
        if not self.x_axis or not self.y_axis:
            return "(empty grid)"

        lines = []
        # 标题
        lines.append(f"Heatmap: {self.x_label} × {self.y_label}")
        lines.append(f"  Values: pass_rate (0.0 = all rejected, 1.0 = all passed)")
        lines.append("")

        # x 轴标签（列头）
        header = f"{'':>12} |"
        for xv in self.x_axis:
            header += f" {_fmt_num(xv):>7} |"
        lines.append(header)
        lines.append("-" * len(header))

        # 每一行
        for yi, yv in enumerate(self.y_axis):
            row = f"{_fmt_num(yv):>12} |"
            for xi in range(len(self.x_axis)):
                val = self._data.get((xi, yi))
                if val is None:
                    row += f" {'N/A':>7} |"
                else:
                    bar = _bar_for_rate(val)
                    row += f" {bar}{val:.2f}{bar} |"
            lines.append(row)
        lines.append("-" * len(header))

        # 图例
        lines.append("")
        lines.append("  Legend: █ = 1.0 ▓ = 0.8 ▒ = 0.6 ░ = 0.4 · = 0.2   = 0.0")

        return "\n".join(lines)

    def render_table(self) -> str:
        """
        渲染为简单表格格式（适合导入 CSV/分析）。
        """
        if not self.x_axis or not self.y_axis:
            return ""

        lines = []
        # CSV header
        header = f"{self.y_label}\\{self.x_label}"
        for xv in self.x_axis:
            header += f",{xv}"
        lines.append(header)

        for yi, yv in enumerate(self.y_axis):
            row = str(yv)
            for xi in range(len(self.x_axis)):
                val = self._data.get((xi, yi))
                if val is None:
                    row += ","
                else:
                    row += f",{val:.4f}"
            lines.append(row)

        return "\n".join(lines)


class HeatmapRunner:
    """
    热力图执行器。

    用法:
        runner = HeatmapRunner(evaluator=my_fn, config=ThresholdConfig(...))
        grid = await runner.run()
    """

    def __init__(
        self,
        evaluator: EvaluatorFn,
        config: ThresholdConfig | None = None,
    ):
        self._evaluator = evaluator
        self._config = config or ThresholdConfig()

    async def run(self) -> HeatmapGrid:
        """
        遍历所有阈值组合，生成热力图网格。
        """
        tc = self._config
        grid = HeatmapGrid(
            x_axis=tc.token_limits,
            y_axis=tc.time_limits_ms,
            x_label="token_limit",
            y_label="time_limit_ms",
        )

        # 为每个 (token_limit, time_limit_ms) 组合运行 evaluator
        for yi, time_ms in enumerate(tc.time_limits_ms):
            for xi, token_limit in enumerate(tc.token_limits):
                thresholds = {
                    "token_limit": token_limit,
                    "time_limit_ms": time_ms,
                    "confidence_threshold": tc.confidence_threshold,
                }

                try:
                    passed, gate_results = await self._evaluator(thresholds)
                    pass_rate = 1.0 if passed else 0.0
                except Exception:
                    pass_rate = 0.0

                grid.set(xi, yi, pass_rate)

        return grid


async def run_heatmap(
    evaluator: EvaluatorFn | None = None,
    token_limits: list[int] | None = None,
    time_limits_ms: list[int] | None = None,
    confidence_threshold: float = 0.8,
    server_url: str = "",
    prompt: str = "",
) -> HeatmapGrid:
    """
    便捷热力图入口。

    当 evaluator 提供时，直接使用（单元测试模式）。

    参数:
        evaluator: 门禁评估函数
        token_limits: x 轴阈值列表
        time_limits_ms: y 轴阈值列表
        confidence_threshold: 置信度阈值
        server_url: API 服务器地址 (预留)
        prompt: 测试用的 prompt (预留)
    """
    if evaluator is not None:
        config = ThresholdConfig(
            token_limits=token_limits or [1000, 5000, 10000, 50000, 100000],
            time_limits_ms=time_limits_ms or [1000, 5000, 10000, 30000, 60000],
            confidence_threshold=confidence_threshold,
        )
        runner = HeatmapRunner(evaluator=evaluator, config=config)
        return await runner.run()

    raise ValueError("evaluator must be provided")


# ── 内部工具函数 ────────────────────────────────────────────────


def _fmt_num(n: int) -> str:
    """格式化数字为简写形式"""
    if n >= 1_000_000:
        return f"{n // 1_000_000}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def _bar_for_rate(rate: float) -> str:
    """根据 pass_rate 返回 ASCII 块字符"""
    if rate >= 0.95:
        return "█"
    elif rate >= 0.85:
        return "▓"
    elif rate >= 0.70:
        return "▒"
    elif rate >= 0.40:
        return "░"
    elif rate >= 0.15:
        return "·"
    else:
        return " "
