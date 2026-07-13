"""Phase 5: Multi-agent trust chain tests."""
from __future__ import annotations

import pytest
from datetime import UTC, datetime, timedelta


# ═══════════════════════════════════════════════════════════════
#  TrustChainValidator — pure logic
# ═══════════════════════════════════════════════════════════════

class TestTrustChainValidator:
    @pytest.fixture
    def validator(self):
        from agent_prod.gates.trust_chain import TrustChainValidator
        return TrustChainValidator()

    def test_no_acl_allows_anything(self, validator):
        # No ACL registered → child unrestricted
        ok, reason = validator.validate_tool_scope("read_file", "child-a")
        assert ok, reason

    def test_restricted_allows_in_allowed_tools(self, validator):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        validator.register_task(TaskACL(
            task_id="t1", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.RESTRICTED,
            allowed_tools={"read_file", "search_files"},
        ))
        ok, _ = validator.validate_tool_scope("read_file", "child")
        assert ok

    def test_restricted_blocks_not_in_allowed_tools(self, validator):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        validator.register_task(TaskACL(
            task_id="t2", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.RESTRICTED,
            allowed_tools={"read_file"},
        ))
        # write_file not in allowed_tools
        # Put agent_type=claude-code to use the canonical resolution
        ok, reason = validator.validate_tool_scope(
            "write_file", "child", agent_type="claude-code",
        )
        assert not ok
        assert "not in any ACL allowed_tools" in reason

    def test_full_trust_inherits_unrestricted(self, validator):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        validator.register_task(TaskACL(
            task_id="t3", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.FULL,
            allowed_tools=set(),
        ))
        ok, _ = validator.validate_tool_scope("write_file", "child",
                                               agent_type="claude-code")
        assert ok

    def test_sandbox_blocks_non_benign(self, validator):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        validator.register_task(TaskACL(
            task_id="t4", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.SANDBOX,
        ))
        # write_file is risky → blocked in sandbox
        ok, reason = validator.validate_tool_scope(
            "write_file", "child", agent_type="claude-code",
        )
        assert not ok
        assert "sandbox" in reason

    def test_expired_acl_skipped(self, validator):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        validator.register_task(TaskACL(
            task_id="t5", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.RESTRICTED,
            allowed_tools={"write_file"},
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        ))
        # Expired ACL is skipped → no constraint → allowed
        ok, _ = validator.validate_tool_scope(
            "write_file", "child", agent_type="claude-code",
        )
        assert ok

    def test_union_of_multiple_acls(self, validator):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        validator.register_task(TaskACL(
            task_id="t6a", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.RESTRICTED,
            allowed_tools={"read_file"},
        ))
        validator.register_task(TaskACL(
            task_id="t6b", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.RESTRICTED,
            allowed_tools={"write_file"},
        ))
        # either ACL allows → union permits
        ok, _ = validator.validate_tool_scope(
            "write_file", "child", agent_type="claude-code",
        )
        assert ok, "union of ACLs should allow write_file"

    def test_domain_scope_restricted(self, validator):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        validator.register_task(TaskACL(
            task_id="t7", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.RESTRICTED,
            allowed_domains={"finance"},
        ))
        ok, _ = validator.validate_domain_scope("finance", "child")
        assert ok
        ok, reason = validator.validate_domain_scope("medical", "child")
        assert not ok
        assert "not in any ACL allowed_domains" in reason


# ═══════════════════════════════════════════════════════════════
#  Gate0 + TrustChain integration
# ═══════════════════════════════════════════════════════════════

class TestGate0WithTrustChain:
    @pytest.fixture
    def gate0_with_acl(self):
        from agent_prod.gates.gate0_permission import Gate0Permission
        from agent_prod.gates.trust_chain import (
            TrustChainValidator, TaskACL, TrustLevel,
        )

        tc = TrustChainValidator()
        tc.register_task(TaskACL(
            task_id="phase5-task", parent_agent="parent", child_agent="child",
            trust_level=TrustLevel.RESTRICTED,
            allowed_tools={"Read", "Grep"},   # claude-code canonical names
        ))
        gate0 = Gate0Permission(config={
            "gates": {"gate0": {"mode": "enforce", "block_unknown_tools": False}}
        }, trust_chain=tc)
        return gate0

    def _make_imp(self, gate0, tool_calls, agent="child", parent="parent",
                  task_id="phase5-task"):
        from agent_prod.gates.models import Improvement
        imp = Improvement(name="phase5-e2e", id="imp-p5-001")
        imp.metadata = {
            "agent": agent,
            "parent_agent": parent,
            "task_id": task_id,
            "declared_tools": list({t["tool_name"] for t in tool_calls}),
            "decisions": [{
                "decision_id": "d1",
                "tool_calls": tool_calls,
            }],
        }
        return imp

    def test_child_reading_allowed(self, gate0_with_acl):
        from agent_prod.gates.models import Improvement
        imp = self._make_imp(gate0_with_acl, [
            {"tool_name": "Read", "arguments": {"file_path": "/tmp/x"}},
        ])
        result = gate0_with_acl.verify(imp)
        assert result.passed, f"Read should be allowed: {result.reason}"

    def test_child_write_blocked_by_trust_chain(self, gate0_with_acl):
        from agent_prod.gates.models import Improvement
        imp = self._make_imp(gate0_with_acl, [
            {"tool_name": "Edit", "arguments": {"file_path": "/tmp/x"}},
        ])
        result = gate0_with_acl.verify(imp)
        assert not result.passed
        # 信任链违规应该出现在 violations 列表
        violations = result.details["violations"]
        assert any(v.get("type") == "trust_chain_violation" for v in violations)

    def test_child_without_parent_agent_unrestricted(self, gate0_with_acl):
        # 无 parent_agent metadata → 不会触发 trust_chain 检查
        from agent_prod.gates.models import Improvement
        imp = Improvement(name="phase5-e2e", id="imp-p5-no-parent")
        imp.metadata = {
            "agent": "child",
            "declared_tools": ["Edit"],
            "decisions": [{
                "decision_id": "d1",
                "tool_calls": [
                    {"tool_name": "Edit", "arguments": {"file_path": "/tmp/x"}},
                ],
            }],
            # 假设有 auth grant 允许 Edit
            "auth_grant_id": "",
            "compliance_claims": {},
        }
        # 即使 ACL 存在，没 parent_agent metadata 时不应用 trust_chain
        result = gate0_with_acl.verify(imp)
        # Edit 是 dangerous，default 拦截逻辑会因 auth 缺失而阻拦
        # 但不应因 trust_chain_violation 拦截
        if not result.passed:
            violations = result.details["violations"]
            assert all(v.get("type") != "trust_chain_violation"
                       for v in violations), \
                "Should not apply trust_chain without parent_agent metadata"


# ═══════════════════════════════════════════════════════════════
#  ACL management basics
# ═══════════════════════════════════════════════════════════════

class TestTaskACL:
    def test_is_expired_when_past_expires_at(self):
        from agent_prod.gates.trust_chain import TaskACL
        acl = TaskACL(
            task_id="x", parent_agent="p", child_agent="c",
            expires_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        assert acl.is_expired

    def test_is_expired_false_when_none(self):
        from agent_prod.gates.trust_chain import TaskACL
        acl = TaskACL(task_id="x", parent_agent="p", child_agent="c")
        assert not acl.is_expired

    def test_to_dict_serialization(self):
        from agent_prod.gates.trust_chain import TaskACL, TrustLevel
        acl = TaskACL(
            task_id="x", parent_agent="p", child_agent="c",
            trust_level=TrustLevel.RESTRICTED,
            allowed_tools={"read_file"},
            allowed_domains={"finance"},
            data_scope="project:test",
        )
        d = acl.to_dict()
        assert d["task_id"] == "x"
        assert d["trust_level"] == "restricted"
        assert d["allowed_tools"] == ["read_file"]
        assert d["allowed_domains"] == ["finance"]
        assert d["data_scope"] == "project:test"
