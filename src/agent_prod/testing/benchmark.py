"""Phase 7.3: Benchmark — 基准快照。

在固定 prompt 集合上运行基准测试，保存快照并对比变化。

用法:
    from agent_prod.testing.benchmark import BenchmarkRunner, BenchmarkSnapshot, save_snapshot, compare_snapshots

    runner = BenchmarkRunner()
    snapshot = runner.run_benchmark(prompts=["What is AI?"], response_fn=my_fn)
    save_snapshot(snapshot, "data/benchmarks/v1.json")
    deltas = compare_snapshots(snap1, snap2)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass
class BenchmarkSnapshot:
    """一次基准测试的快照。"""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    avg_turns: float = 0.0
    avg_duration_ms: float = 0.0
    avg_tokens: float = 0.0
    gate_pass_rate: float = 0.0
    prompt_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "avg_turns": self.avg_turns,
            "avg_duration_ms": self.avg_duration_ms,
            "avg_tokens": self.avg_tokens,
            "gate_pass_rate": self.gate_pass_rate,
            "prompt_count": self.prompt_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BenchmarkSnapshot":
        return cls(
            timestamp=d.get("timestamp", ""),
            avg_turns=d.get("avg_turns", 0.0),
            avg_duration_ms=d.get("avg_duration_ms", 0.0),
            avg_tokens=d.get("avg_tokens", 0.0),
            gate_pass_rate=d.get("gate_pass_rate", 0.0),
            prompt_count=d.get("prompt_count", 0),
            metadata=d.get("metadata", {}),
        )


class BenchmarkRunner:
    """基准测试运行器。

    在固定 prompt 集合上运行，收集指标并生成快照。

    response_fn 签名: (prompt: str) -> dict with keys:
        turns, duration_ms, tokens, gate_pass
    """

    def __init__(self):
        self._last_run_time_ms: float = 0.0

    def run_benchmark(
        self,
        prompts: list[str],
        response_fn: Callable[[str], dict[str, Any]],
    ) -> BenchmarkSnapshot:
        """在固定 prompt 集上运行基准测试。

        参数:
            prompts: 测试 prompt 列表
            response_fn: 响应函数，接受 prompt 返回含指标的 dict

        返回:
            聚合后的 BenchmarkSnapshot
        """
        if not prompts:
            return BenchmarkSnapshot(prompt_count=0)

        total_turns = 0
        total_duration_ms = 0.0
        total_tokens = 0
        gate_passed = 0

        start = time.monotonic()
        for prompt in prompts:
            result = response_fn(prompt)
            total_turns += result.get("turns", 0)
            total_duration_ms += result.get("duration_ms", 0)
            total_tokens += result.get("tokens", 0)
            if result.get("gate_pass", False):
                gate_passed += 1

        self._last_run_time_ms = (time.monotonic() - start) * 1000

        n = len(prompts)
        snapshot = BenchmarkSnapshot(
            avg_turns=round(total_turns / n, 2),
            avg_duration_ms=round(total_duration_ms / n, 2),
            avg_tokens=round(total_tokens / n, 2),
            gate_pass_rate=round(gate_passed / n, 4),
            prompt_count=n,
        )
        return snapshot


def save_snapshot(snapshot: BenchmarkSnapshot, path: str) -> None:
    """将 BenchmarkSnapshot 保存到 JSON 文件。

    参数:
        snapshot: 要保存的快照
        path: 输出文件路径
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(snapshot.to_dict(), f, indent=2, ensure_ascii=False)


def load_snapshot(path: str) -> BenchmarkSnapshot | None:
    """从 JSON 文件加载 BenchmarkSnapshot。"""
    file_path = Path(path)
    if not file_path.exists():
        return None
    with open(file_path, "r") as f:
        data = json.load(f)
    return BenchmarkSnapshot.from_dict(data)


def compare_snapshots(
    s1: BenchmarkSnapshot,
    s2: BenchmarkSnapshot,
) -> dict[str, Any]:
    """比较两个 BenchmarkSnapshot 的差异。

    参数:
        s1: 旧快照（基线）
        s2: 新快照（候选）

    返回:
        {
            "avg_turns_delta": float,
            "avg_duration_ms_delta": float,
            "avg_tokens_delta": float,
            "gate_pass_rate_delta": float,
            "prompt_count_match": bool,
            "improved": bool,   # token 和 time 都减少
            "details": list[str],
        }
    """
    details: list[str] = []

    turns_delta = round(s2.avg_turns - s1.avg_turns, 2)
    duration_delta = round(s2.avg_duration_ms - s1.avg_duration_ms, 2)
    tokens_delta = round(s2.avg_tokens - s1.avg_tokens, 2)
    gate_delta = round(s2.gate_pass_rate - s1.gate_pass_rate, 4)

    if abs(turns_delta) > 0:
        direction = "more" if turns_delta > 0 else "fewer"
        details.append(f"Turns: {direction} by {abs(turns_delta)}")

    if abs(duration_delta) > 0:
        direction = "slower" if duration_delta > 0 else "faster"
        details.append(f"Duration: {direction} by {abs(duration_delta)}ms")

    if abs(tokens_delta) > 0:
        direction = "more" if tokens_delta > 0 else "fewer"
        details.append(f"Tokens: {direction} by {abs(tokens_delta)}")

    if abs(gate_delta) > 0:
        direction = "improved" if gate_delta > 0 else "declined"
        details.append(f"Gate pass rate: {direction} by {abs(gate_delta):.1%}")

    # "improved" means tokens down AND duration down
    improved = tokens_delta <= 0 and duration_delta <= 0

    return {
        "avg_turns_delta": turns_delta,
        "avg_duration_ms_delta": duration_delta,
        "avg_tokens_delta": tokens_delta,
        "gate_pass_rate_delta": gate_delta,
        "prompt_count_match": s1.prompt_count == s2.prompt_count,
        "improved": improved,
        "details": details if details else ["No significant changes"],
    }
