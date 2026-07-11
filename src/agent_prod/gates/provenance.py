# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""数据溯源模型 — 追踪每个输出字段的来源。

记录 Improvement 中每个 candidate_output / baseline_output 字段来自
哪个 Gate、哪个工具调用、哪个计算步骤。满足金融/能源行业对"每个数字
必须有来源"的监管要求。

设计决策:
  - 按 field 索引，一个字段可以有多个来源（如 latency 同时来自工具耗时和基线）
  - 溯源数据随 Improvement 一起持久化，不单独存储
  - 缺失溯源不阻断，只记录；Gate6 可基于此生成溯源检查结果
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class ProvenanceEntry(BaseModel):
    """一条数据溯源记录"""
    field: str               # 字段名，如 "latency_p95_ms"
    value: Any = None        # 字段值
    source: str = ""         # 来源描述，如 "tool:read_file", "gate6_checklist"
    source_id: str = ""      # 来源 ID，如具体工具调用 ID
    computed_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    model_config = {"extra": "ignore"}


class DataProvenance(BaseModel):
    """数据溯源集合"""
    improvement_id: str = ""
    entries: dict[str, list[ProvenanceEntry]] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}

    def record(
        self,
        field: str,
        value: Any,
        source: str,
        source_id: str = "",
        confidence: float = 1.0,
    ) -> None:
        if field not in self.entries:
            self.entries[field] = []
        self.entries[field].append(ProvenanceEntry(
            field=field,
            value=value,
            source=source,
            source_id=source_id,
            confidence=confidence,
        ))

    def get_source(self, field: str) -> list[ProvenanceEntry]:
        """返回指定字段的所有来源"""
        return self.entries.get(field, [])

    def to_report(self) -> str:
        if not self.entries:
            return f"DataProvenance[{self.improvement_id}]: no provenance recorded"
        lines = [
            f"DataProvenance: {self.improvement_id}",
            f"  Tracked fields: {len(self.entries)}",
            "",
        ]
        for field, entries in sorted(self.entries.items()):
            lines.append(f"  {field}:")
            for e in entries:
                lines.append(f"    └─ {e.source} (conf={e.confidence:.2f})")
        return "\n".join(lines)