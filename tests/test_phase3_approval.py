"""Phase 3: Async approval queue + Gate5 pending_approval tests."""
from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════
#  ApprovalQueue — pure queue semantics
# ═══════════════════════════════════════════════════════════════

class TestApprovalQueue:
    def test_request_approve_reject_flow(self):
        from agent_prod.gates.approval import ApprovalQueue
        from agent_prod.gates.models import Improvement

        q = ApprovalQueue()
        imp = Improvement(name="t", id="imp-t-001")
        imp.metadata["agent"] = "qclaw"
        rec = q.request(imp, remaining_gates=["gate6_answer_quality"],
                        requested_by="tester")

        assert rec.is_pending
        assert rec.remaining_gates == ["gate6_answer_quality"]
        assert rec.agent == "qclaw"
        assert q.get(rec.approval_id) is rec
        assert q.get_by_improvement(imp.id) is rec

        assert q.approve(rec.approval_id, "alice", "ok") is not None
        assert not rec.is_pending
        assert rec.decided_by == "alice"

        # Idempotent: cannot decide twice
        assert q.approve(rec.approval_id, "bob", "late") is None

    def test_reject(self):
        from agent_prod.gates.approval import ApprovalQueue
        from agent_prod.gates.models import Improvement

        q = ApprovalQueue()
        imp = Improvement(name="t", id="imp-t-002")
        rec = q.request(imp, remaining_gates=[])
        q.reject(rec.approval_id, "bob", "nope")
        assert rec.status.value == "rejected"
        assert rec.decision_reason == "nope"

    def test_idempotent_request(self):
        from agent_prod.gates.approval import ApprovalQueue
        from agent_prod.gates.models import Improvement

        q = ApprovalQueue()
        imp = Improvement(name="t", id="imp-t-003")
        r1 = q.request(imp, remaining_gates=["x"], requested_by="t")
        r2 = q.request(imp, remaining_gates=["x"], requested_by="t")
        # Same record (pending state)
        assert r1 is r2

    def test_list_pending_filters_by_agent(self):
        from agent_prod.gates.approval import ApprovalQueue
        from agent_prod.gates.models import Improvement

        q = ApprovalQueue()
        i1 = Improvement(name="t", id="i1"); i1.metadata["agent"] = "alice"
        i2 = Improvement(name="t", id="i2"); i2.metadata["agent"] = "bob"
        q.request(i1, remaining_gates=[])
        q.request(i2, remaining_gates=[])
        assert len(q.list_pending()) == 2
        assert len(q.list_pending(agent="alice")) == 1
        assert q.list_pending(agent="alice")[0].agent == "alice"


# ═══════════════════════════════════════════════════════════════
#  Gate5 — pending_approval detection
# ═══════════════════════════════════════════════════════════════

class TestGate5PendingApproval:
    def test_enforce_emits_pending_when_only_human_missing(self):
        from agent_prod.gates.gate5_audit import Gate5ReleaseAudit, Gate5Config
        from agent_prod.gates.models import Improvement, GateName, GateResult

        g5 = Gate5ReleaseAudit(config=Gate5Config(mode="enforce", skip_human_approval=False))
        imp = Improvement(name="t", id="imp-pa-001")
        for gn in (GateName.GATE1, GateName.GATE2, GateName.GATE3, GateName.GATE4):
            imp.add_result(GateResult(gate_name=gn, passed=True))
        imp.llm_calls = [{"response_id": "r1"}]
        result = g5.verify(imp)
        assert result.passed, "Gate5 should pass — only human approval missing"
        assert result.details["pending_approval"] is True
        assert "Pending human approval" in result.reason

    def test_enforce_still_rejects_other_critical_failures(self):
        from agent_prod.gates.gate5_audit import Gate5ReleaseAudit, Gate5Config
        from agent_prod.gates.models import Improvement, GateName, GateResult

        g5 = Gate5ReleaseAudit(config=Gate5Config(mode="enforce", skip_human_approval=False))
        imp = Improvement(name="t", id="imp-pa-002")
        # 仅通过 G1+G2，G3+G4 缺失 -> 要求前置门满足的 critical 规则会失败
        imp.add_result(GateResult(gate_name=GateName.GATE1, passed=True))
        imp.add_result(GateResult(gate_name=GateName.GATE2, passed=True))
        result = g5.verify(imp)
        # G3+G4 没过 + 人工审批也没过 -> 2 个 critical 失败 -> 不 emit pending, 直接 fail
        assert not result.passed
        assert not result.details.get("pending_approval")
        # 应该有至少 2 个 critical 违规
        assert len(result.details["critical_violations"]) >= 2

    def test_observe_no_pending(self):
        from agent_prod.gates.gate5_audit import Gate5ReleaseAudit, Gate5Config
        from agent_prod.gates.models import Improvement, GateName, GateResult

        g5 = Gate5ReleaseAudit(config=Gate5Config(mode="observe"))
        imp = Improvement(name="t", id="imp-pa-003")
        for gn in (GateName.GATE1, GateName.GATE2, GateName.GATE3, GateName.GATE4):
            imp.add_result(GateResult(gate_name=gn, passed=True))
        result = g5.verify(imp)
        assert result.passed
        # observe 模式下不应触发 pending_approval
        assert not result.details.get("pending_approval")


# ═══════════════════════════════════════════════════════════════
#  Engine.resume_after_approval — e2e
# ═══════════════════════════════════════════════════════════════

class TestResumeAfterApproval:
    @pytest.fixture
    def engine_with_enforce(self):
        from agent_prod.gates.engine import QualityGateEngine
        from agent_prod.gates.repository import MemoryRepository
        import yaml

        with open("src/agent_prod/gates/config.yaml") as f:
            cfg = yaml.safe_load(f)
        cfg["gates"]["gate5"]["mode"] = "enforce"
        cfg["gates"]["gate5"]["skip_human_approval"] = False
        e = QualityGateEngine(config=cfg)
        e.repository = MemoryRepository()
        return e

    def _make_imp(self, _id):
        from agent_prod.gates.models import Improvement
        imp = Improvement(name="phase3-e2e", id=_id)
        imp.metadata = {
            "agent": "qclaw",
            "domain": "",
            "declared_tools": [],
            "decisions": [{"decision_id": "d1", "tool_calls": []}],
        }
        imp.candidate_output = {"final_response": "phase3 e2e"}
        return imp

    def test_pending_then_approve_produces_production(self, engine_with_enforce):
        e = engine_with_enforce
        imp = self._make_imp("imp-resume-001")
        result = e.run_pipeline(imp, persist=True)
        assert result.status.value == "pending_approval"

        recs = e.approval_queue.list_pending()
        assert len(recs) == 1
        aid = recs[0].approval_id

        final = e.resume_after_approval(aid, "alice", True, "ok", persist=True)
        assert final.status.value == "production"
        assert final.human_approver == "alice"
        assert final.human_approved_at is not None

    def test_pending_then_reject_marks_rejected(self, engine_with_enforce):
        e = engine_with_enforce
        imp = self._make_imp("imp-resume-002")
        e.run_pipeline(imp, persist=True)

        recs = e.approval_queue.list_pending()
        aid = recs[0].approval_id
        final = e.resume_after_approval(aid, "bob", False, "no go", persist=True)
        assert final.status.value == "rejected"
        assert final.fail_gate == "gate5_approval"
        # Improvement 中写入 approver 但 approved_at 应为 None
        assert final.human_approver == "bob"

    def test_resume_idempotent_after_decision(self, engine_with_enforce):
        e = engine_with_enforce
        imp = self._make_imp("imp-resume-003")
        e.run_pipeline(imp, persist=True)
        recs = e.approval_queue.list_pending()
        aid = recs[0].approval_id
        e.resume_after_approval(aid, "alice", True, "ok", persist=True)
        # 再次决策应抛 ValueError
        with pytest.raises(ValueError, match="already approved"):
            e.resume_after_approval(aid, "eve", True, "redo", persist=True)

    def test_resume_unknown_approval_raises(self, engine_with_enforce):
        with pytest.raises(ValueError, match="not found"):
            engine_with_enforce.resume_after_approval(
                "appr-nonexistent", "alice", True, "", persist=True
            )
