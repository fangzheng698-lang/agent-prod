"""Phase 7.2: Replay — 录制+回放机制。

记录每次 Agent 执行的完整对话轮次，支持事后重放和对比。

用法:
    from agent_prod.testing.replay import ReplayRecorder, ReplayPlayer, ReplayRecord

    recorder = ReplayRecorder()
    recorder.record("run_001", turns, final_response="Hello world")

    player = ReplayPlayer()
    replay = player.load("run_001")
    diff = player.diff(expected, actual)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ReplayRecord:
    """单次执行的完整回放记录。"""
    run_id: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    turns: list[dict[str, Any]] = field(default_factory=list)
    final_response: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "turns": self.turns,
            "final_response": self.final_response,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplayRecord":
        return cls(
            run_id=d.get("run_id", ""),
            timestamp=d.get("timestamp", ""),
            turns=d.get("turns", []),
            final_response=d.get("final_response", ""),
            metadata=d.get("metadata", {}),
        )


class ReplayRecorder:
    """录制器：将执行记录保存到文件。

    保存路径: data/replays/{run_id}.json
    """

    def __init__(self, base_dir: str = "data/replays"):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        run_id: str,
        turns: list[dict[str, Any]],
        final_response: str = "",
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ReplayRecord:
        """录制一次执行并保存到文件。

        参数:
            run_id: 执行唯一标识
            turns: 对话轮次列表，每轮为 dict
            final_response: 最终响应内容
            metadata: 额外元数据

        返回:
            创建的 ReplayRecord
        """
        record = ReplayRecord(
            run_id=run_id,
            turns=turns,
            final_response=final_response,
            metadata=metadata or {},
        )
        path = self._base_dir / f"{run_id}.json"
        with open(path, "w") as f:
            json.dump(record.to_dict(), f, indent=2, ensure_ascii=False)
        return record

    def get_path(self, run_id: str) -> Path:
        """获取指定 run_id 的存储路径。"""
        return self._base_dir / f"{run_id}.json"

    def exists(self, run_id: str) -> bool:
        """检查 replay 记录是否存在。"""
        return self.get_path(run_id).exists()

    def list_records(self) -> list[str]:
        """列出所有已录制的 run_id。"""
        if not self._base_dir.exists():
            return []
        return sorted(
            f.stem for f in self._base_dir.glob("*.json")
        )


class ReplayPlayer:
    """回放器：加载重放记录并对比差异。"""

    def __init__(self, base_dir: str = "data/replays"):
        self._base_dir = Path(base_dir)

    def load(self, run_id: str) -> ReplayRecord | None:
        """加载指定 run_id 的重放记录。

        参数:
            run_id: 执行唯一标识

        返回:
            ReplayRecord 或 None（如果不存在）
        """
        path = self._base_dir / f"{run_id}.json"
        if not path.exists():
            return None
        with open(path, "r") as f:
            data = json.load(f)
        return ReplayRecord.from_dict(data)

    def diff(
        self,
        expected: ReplayRecord,
        actual: ReplayRecord,
    ) -> dict[str, Any]:
        """比较两个 ReplayRecord 的差异。

        参数:
            expected: 期望的重放记录
            actual: 实际的重放记录

        返回:
            {
                "match": bool,
                "final_response_match": bool,
                "turn_count_match": bool,
                "turn_count_diff": int,
                "diffs": list[str],
            }
        """
        diffs: list[str] = []

        final_response_match = (
            expected.final_response.strip() == actual.final_response.strip()
        )
        if not final_response_match:
            diffs.append(
                f"final_response mismatch: "
                f"expected={expected.final_response[:80]}..., "
                f"actual={actual.final_response[:80]}..."
            )

        turn_count_match = len(expected.turns) == len(actual.turns)
        if not turn_count_match:
            diffs.append(
                f"turn count mismatch: "
                f"expected={len(expected.turns)}, actual={len(actual.turns)}"
            )

        # 逐轮对比
        for i in range(min(len(expected.turns), len(actual.turns))):
            et = expected.turns[i]
            at = actual.turns[i]
            if et != at:
                diffs.append(f"turn[{i}] content differs")

        for i in range(len(expected.turns), len(actual.turns)):
            diffs.append(f"turn[{i}] extra in actual")
        for i in range(len(actual.turns), len(expected.turns)):
            diffs.append(f"turn[{i}] missing from actual")

        return {
            "match": len(diffs) == 0,
            "final_response_match": final_response_match,
            "turn_count_match": turn_count_match,
            "turn_count_diff": len(actual.turns) - len(expected.turns),
            "diffs": diffs,
        }
