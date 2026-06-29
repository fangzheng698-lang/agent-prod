"""Tests for ProxySession and ProxySessionManager."""
from __future__ import annotations

import time
import threading

import pytest

from agent_prod.server.proxy_session import (
    ProxySession,
    ProxySessionManager,
    SessionStatus,
)


class TestProxySession:
    def test_session_creation(self):
        session = ProxySession(
            session_id="pxy_test_1",
            agent_type="claude-code",
            version="2.1.150",
            model="claude-sonnet-4-6",
        )
        assert session.session_id == "pxy_test_1"
        assert session.status == SessionStatus.ACTIVE
        assert session.accumulated_decisions == 0

    def test_record_turn(self):
        session = ProxySession(session_id="pxy_test_2", agent_type="test")
        session.record_turn(
            model="claude-sonnet-4-6",
            prompt_tokens=100,
            completion_tokens=50,
            tool_calls=[{"tool_id": "tc-1", "tool_name": "read_file"}],
            latency_ms=500.0,
        )
        assert session.accumulated_decisions == 1
        assert session.total_prompt_tokens == 100
        assert session.total_completion_tokens == 50
        assert session.total_duration_ms == 500.0

    def test_record_multiple_turns(self):
        session = ProxySession(session_id="pxy_test_3", agent_type="test")
        for i in range(3):
            session.record_turn(
                model="claude-sonnet-4-6",
                prompt_tokens=10,
                completion_tokens=5,
                tool_calls=[],
                latency_ms=100.0,
            )
        assert session.accumulated_decisions == 3
        assert session.total_prompt_tokens == 30
        assert session.total_completion_tokens == 15
        assert session.total_duration_ms == 300.0

    def test_is_stale(self):
        session = ProxySession(session_id="pxy_test_4", agent_type="test")
        session.last_seen = time.time() - 200  # 200s ago
        assert session.is_stale(timeout_seconds=120)

    def test_not_stale(self):
        session = ProxySession(session_id="pxy_test_5", agent_type="test")
        session.last_seen = time.time()  # just now
        assert not session.is_stale(timeout_seconds=120)

    def test_build_final_trace(self):
        session = ProxySession(
            session_id="pxy_trace", agent_type="claude-code",
            version="2.1.150",
        )
        session.set_declared_tools(["Read", "Write"], [])
        session.record_turn(
            model="claude-sonnet-4-6",
            prompt_tokens=100, completion_tokens=50,
            tool_calls=[{"tool_id": "tc-1", "tool_name": "Read"}],
            latency_ms=200,
        )
        session.final_output = "done"
        trace = session.build_final_trace()
        assert trace["agent"] == "claude-code"
        assert trace["session_id"] == "pxy_trace"
        assert len(trace["decisions"]) == 1
        assert trace["declared_tools"] == ["Read", "Write"]
        assert trace["output"]["final_response"] == "done"
        assert trace["current_metrics"]["tokens_total"] == 150
        assert trace["current_metrics"]["custom"]["total_turns"] == 1

    def test_to_dict(self):
        session = ProxySession(session_id="pxy_dict", agent_type="test")
        session.record_turn(model="m", prompt_tokens=10, completion_tokens=5, tool_calls=[], latency_ms=50)
        d = session.to_dict()
        assert d["session_id"] == "pxy_dict"
        assert d["decisions_count"] == 1
        assert d["total_prompt_tokens"] == 10


class TestProxySessionManager:
    def test_get_or_create(self):
        mgr = ProxySessionManager()
        s = mgr.get_or_create("s1", "claude-code", version="1.0")
        assert s.session_id == "s1"
        assert s.agent_type == "claude-code"

    def test_get_or_create_returns_existing(self):
        mgr = ProxySessionManager()
        s1 = mgr.get_or_create("s1", "claude-code")
        s2 = mgr.get_or_create("s1", "claude-code")
        assert s1 is s2

    def test_get_returns_none_for_missing(self):
        mgr = ProxySessionManager()
        assert mgr.get("nonexistent") is None

    def test_finalize(self):
        mgr = ProxySessionManager()
        mgr.get_or_create("s_final", "test")
        mgr.finalize("s_final", SessionStatus.COMPLETED, output="done")
        s = mgr.get("s_final")
        assert s is not None
        assert s.status == SessionStatus.COMPLETED
        assert s.final_output == "done"

    def test_finalize_missing_session(self):
        mgr = ProxySessionManager()
        result = mgr.finalize("nonexistent", SessionStatus.COMPLETED)
        assert result is None

    def test_list_active(self):
        mgr = ProxySessionManager()
        mgr.get_or_create("a1", "test")
        mgr.get_or_create("a2", "test")
        mgr.finalize("a2", SessionStatus.COMPLETED)
        active = mgr.list_active()
        assert len(active) == 1
        assert active[0].session_id == "a1"

    def test_detect_stale(self):
        mgr = ProxySessionManager()
        s = mgr.get_or_create("stale_s", "test")
        s.last_seen = time.time() - 300  # 5 minutes ago
        stale = mgr.detect_stale(timeout_seconds=120)
        assert len(stale) == 1
        assert stale[0].session_id == "stale_s"
        assert stale[0].status == SessionStatus.CRASHED

    def test_detect_stale_skips_completed(self):
        mgr = ProxySessionManager()
        s = mgr.get_or_create("done_s", "test")
        s.last_seen = time.time() - 300
        mgr.finalize("done_s", SessionStatus.COMPLETED)
        stale = mgr.detect_stale(timeout_seconds=120)
        assert len(stale) == 0

    def test_pop_for_evaluation(self):
        mgr = ProxySessionManager()
        mgr.get_or_create("eval_me", "test")
        mgr.finalize("eval_me", SessionStatus.COMPLETED)
        ready = mgr.pop_for_evaluation()
        assert len(ready) == 1
        assert ready[0].session_id == "eval_me"
        # Session should be removed from active
        assert mgr.get("eval_me") is None

    def test_pop_for_evaluation_skips_unfinished(self):
        mgr = ProxySessionManager()
        mgr.get_or_create("active_s", "test")
        ready = mgr.pop_for_evaluation()
        assert len(ready) == 0

    def test_pop_for_evaluation_skips_already_evaluated(self):
        mgr = ProxySessionManager()
        s = mgr.get_or_create("evaled", "test")
        mgr.finalize("evaled", SessionStatus.COMPLETED)
        s.gate_result = {"passed": True}
        ready = mgr.pop_for_evaluation()
        assert len(ready) == 0  # has gate_result, should stay

    def test_set_gate_result(self):
        mgr = ProxySessionManager()
        mgr.get_or_create("gate_r", "test")
        mgr.set_gate_result("gate_r", {"passed": True, "status": "production"})
        s = mgr.get("gate_r")
        assert s is not None
        assert s.gate_result == {"passed": True, "status": "production"}

    def test_remove(self):
        mgr = ProxySessionManager()
        mgr.get_or_create("remove_me", "test")
        mgr.remove("remove_me")
        assert mgr.get("remove_me") is None

    def test_concurrent_access(self):
        """Multiple threads can get_or_create and record_turn on same session."""
        mgr = ProxySessionManager()
        errors = []
        barrier = threading.Barrier(10, timeout=5)

        def worker():
            try:
                s = mgr.get_or_create("concurrent", "test")
                s.record_turn(model="m", prompt_tokens=10, completion_tokens=5, tool_calls=[], latency_ms=50)
                barrier.wait()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        s = mgr.get("concurrent")
        assert s is not None
        assert s.accumulated_decisions == 10

    def test_list_all(self):
        mgr = ProxySessionManager()
        mgr.get_or_create("l1", "test")
        mgr.get_or_create("l2", "test")
        all_s = mgr.list_all()
        assert len(all_s) == 2

    def test_crashed_session_re_activated(self):
        mgr = ProxySessionManager()
        s = mgr.get_or_create("crash_revive", "test")
        s.status = SessionStatus.CRASHED
        # get_or_create should re-activate
        s2 = mgr.get_or_create("crash_revive", "test")
        assert s2.status == SessionStatus.ACTIVE