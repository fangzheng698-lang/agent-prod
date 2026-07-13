"""Async approval queue for Gate5.

Decouples the pipeline from human-in-the-loop approval:

    pipeline → gate1..gate4 → gate5.verify()
                                      │
                                      ├── all critical rules pass → continue
                                      ├── only failing rule is "Human approval" →
                                      │       emit PENDING_APPROVAL status,
                                      │       persist improvement, exit pipeline
                                      └── other critical failure → REJECTED as usual

    External webhook → ApprovalQueue.approve(improvement_id, approver)
                                      │
                                      └── resume pipeline (gate6, gate7, persist)

Persistence: ApprovalRecord stored in repository alongside Improvement
so state survives process restart. ApprovalQueue is the in-process broker;
the server layer (FastAPI) exposes /v1/approvals/{id}/approve and emits a
webhook callback if configured.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .models import GateName, Improvement, ImprovementStatus

logger = logging.getLogger(__name__)


# ── Status enum for approval records ──────────────────────

class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ── Approval Record ──────────────────────────────────────

@dataclass
class ApprovalRecord:
    """A single pending approval request."""
    approval_id: str
    improvement_id: str
    agent: str
    requested_at: datetime
    requested_by: str = "system"
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str = ""
    # The gate that emitted the pending state (currently always gate5)
    gate: str = "gate5"
    # What remains to run after approval — preserved across restarts
    remaining_gates: list[str] = field(default_factory=list)
    # Webhook URL to call once approved (per-record override)
    webhook_url: str | None = None
    # Free-form metadata — domain, tool, risk info, etc.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING

    @property
    def age_seconds(self) -> float:
        end = self.decided_at or datetime.now(UTC)
        return (end - self.requested_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "improvement_id": self.improvement_id,
            "agent": self.agent,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "requested_by": self.requested_by,
            "status": self.status.value,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "decision_reason": self.decision_reason,
            "gate": self.gate,
            "remaining_gates": list(self.remaining_gates),
            "webhook_url": self.webhook_url,
            "metadata": dict(self.metadata),
            "age_seconds": self.age_seconds,
        }


# ── Approval Queue ──────────────────────────────────────

WebhookFn = Callable[[ApprovalRecord], None]


class ApprovalQueue:
    """In-process approval broker.

    In a multi-node deployment, swap this for a Redis/Postgres-backed queue
    (the ApprovalRecord schema is serialization-safe). For single-node
    production and CI use, the in-process queue is sufficient.
    """

    def __init__(self, webhook_fn: WebhookFn | None = None,
                 ttl_seconds: int = 86400):
        self._records: dict[str, ApprovalRecord] = {}
        self._by_improvement: dict[str, str] = {}  # improvement_id -> approval_id
        self._webhook_fn = webhook_fn
        self._ttl_seconds = ttl_seconds

    # ── Internal helpers ──

    def _expire_stale(self) -> None:
        cutoff = datetime.now(UTC).timestamp() - self._ttl_seconds
        stale = [
            rid for rid, rec in self._records.items()
            if rec.is_pending
            and rec.requested_at.timestamp() < cutoff
        ]
        for rid in stale:
            rec = self._records[rid]
            rec.status = ApprovalStatus.EXPIRED
            rec.decided_at = datetime.now(UTC)
            rec.decision_reason = "auto-expired (TTL)"
            logger.info("Approval %s expired after %ds TTL", rid, self._ttl_seconds)

    # ── Public API ──

    def request(self, improvement: Improvement,
                remaining_gates: list[str],
                requested_by: str = "system",
                webhook_url: str | None = None,
                metadata: dict[str, Any] | None = None) -> ApprovalRecord:
        """Register a pending approval request. Returns the record.

        Caller (engine) is expected to have already persisted the Improvement
        with status PENDING_APPROVAL before calling.
        """
        self._expire_stale()
        # Reuse existing pending approval for the same improvement (idempotent)
        existing_id = self._by_improvement.get(improvement.id)
        if existing_id and existing_id in self._records:
            rec = self._records[existing_id]
            if rec.is_pending:
                return rec

        approval_id = f"appr-{uuid.uuid4().hex[:12]}"
        record = ApprovalRecord(
            approval_id=approval_id,
            improvement_id=improvement.id,
            agent=improvement.metadata.get("agent", "unknown"),
            requested_at=datetime.now(UTC),
            requested_by=requested_by,
            remaining_gates=list(remaining_gates),
            webhook_url=webhook_url,
            metadata=dict(metadata or {}),
        )
        self._records[approval_id] = record
        self._by_improvement[improvement.id] = approval_id
        logger.info("Approval requested: %s for improvement %s (agent=%s)",
                    approval_id, improvement.id, record.agent)
        return record

    def approve(self, approval_id: str, approver: str,
                reason: str = "") -> ApprovalRecord | None:
        """Mark approval as approved. Returns the record, or None if not found
        or already decided."""
        self._expire_stale()
        rec = self._records.get(approval_id)
        if not rec or not rec.is_pending:
            return None
        rec.status = ApprovalStatus.APPROVED
        rec.decided_at = datetime.now(UTC)
        rec.decided_by = approver
        rec.decision_reason = reason
        logger.info("Approval %s granted by %s", approval_id, approver)
        # Fire webhook side-effect (don't let it bubble errors into caller)
        if self._webhook_fn:
            try:
                self._webhook_fn(rec)
            except Exception as e:
                logger.error("Approval webhook failed for %s: %s",
                             approval_id, e)
        return rec

    def reject(self, approval_id: str, approver: str,
               reason: str = "") -> ApprovalRecord | None:
        """Mark approval as rejected. Pipeline will resume and route to reject
        rather than continue."""
        self._expire_stale()
        rec = self._records.get(approval_id)
        if not rec or not rec.is_pending:
            return None
        rec.status = ApprovalStatus.REJECTED
        rec.decided_at = datetime.now(UTC)
        rec.decided_by = approver
        rec.decision_reason = reason
        logger.info("Approval %s rejected by %s: %s",
                    approval_id, approver, reason)
        if self._webhook_fn:
            try:
                self._webhook_fn(rec)
            except Exception as e:
                logger.error("Approval webhook failed for %s: %s",
                             approval_id, e)
        return rec

    def get(self, approval_id: str) -> ApprovalRecord | None:
        self._expire_stale()
        return self._records.get(approval_id)

    def get_by_improvement(self, improvement_id: str) -> ApprovalRecord | None:
        self._expire_stale()
        aid = self._by_improvement.get(improvement_id)
        return self._records.get(aid) if aid else None

    def list_pending(self, agent: str | None = None) -> list[ApprovalRecord]:
        self._expire_stale()
        recs = [r for r in self._records.values() if r.is_pending]
        if agent:
            recs = [r for r in recs if r.agent == agent]
        return sorted(recs, key=lambda r: r.requested_at)

    def list_all(self) -> list[ApprovalRecord]:
        self._expire_stale()
        return sorted(self._records.values(),
                      key=lambda r: r.requested_at, reverse=True)


# ── Default webhook (logs + persists) ──────────────────────

def default_webhook(record: ApprovalRecord) -> None:
    """Default webhook: log only.

    In production, the server layer wires a different webhook that calls the
    external service configured in config.yaml gates.gate5.webhook_url or
    Improvement.metadata["approval_webhook_url"].
    """
    logger.info("[webhook] approval %s -> %s (by %s)",
                record.approval_id, record.status.value,
                record.decided_by or "?")
