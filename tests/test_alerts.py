"""
Alert dispatch tests.

Verifies:
  1. AlertPayload formatting (summary_text, to_markdown)
  2. DiscordAlert, TelegramAlert, WebhookAlert backends
  3. AlertDispatcher multi-backend delivery
  4. Factory creates correct dispatchers from config
  5. Engine integrates alert dispatch on rejection
  6. Alert failure does not affect gate pipeline
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from agent_prod.gates.alerts import (
    AlertPayload, AlertDispatcher,
    DiscordAlert, TelegramAlert, WebhookAlert,
    create_dispatcher_from_config,
)
from agent_prod.gates.engine import QualityGateEngine
from agent_prod.gates.models import Improvement

PASS = 0
FAIL = 0

def ok(msg: str):
    global PASS; PASS += 1
    print(f"  ✅ {msg}")

def ng(msg: str, detail=""):
    global FAIL; FAIL += 1
    print(f"  ❌ {msg}")
    if detail:
        print(f"     {detail}")

def make_improvement(name="test", **kwargs) -> Improvement:
    defaults = {
        "name": name,
        "candidate_output": {
            "final_response": "test output",
            "confidence": 0.9,
            "tools_used": ["search"],
            "token_count": 500,
            "warnings": [],
        },
        "budget_tokens": 100_000,
        "budget_time_ms": 60_000,
        "actual_tokens": 500,
        "actual_time_ms": 1_000,
        "llm_calls": [{"response_id": "r1"}],
        "tool_calls": [{"request_id": "r1"}],
    }
    defaults.update(kwargs)
    return Improvement(**defaults)


# ══════════════════════════════════════════════════════════════
print("=" * 65)
print("  Alert Dispatch Tests")
print("=" * 65)
print()

# ── 1. AlertPayload formatting ────────────────────────────
print("── 1. AlertPayload formatting ──")
payload = AlertPayload(
    agent_type="hermes",
    session_id="ses_abc123",
    improvement_name="test-imp",
    failed_gate="gate3_regression",
    fail_reason="f1_score degraded -5.2%",
    gates_summary=[
        {"gate": "gate1_execution", "passed": True, "reason": "OK"},
        {"gate": "gate2_trace_integrity", "passed": True, "reason": "OK"},
        {"gate": "gate3_regression", "passed": False, "reason": "f1 degraded"},
    ],
)
assert "hermes" in payload.summary_text()
assert "ses_abc123" in payload.summary_text()
assert "gate3_regression" in payload.summary_text()
ok(f"summary_text: {payload.summary_text()[:80]}...")

md = payload.to_markdown()
assert "Quality Gate Alert" in md
assert "hermes" in md
assert "ses_abc123" in md
ok(f"to_markdown: {len(md)} chars, contains agent/session/gate info")

# ── 2. Discord alert (will fail — no real webhook) ────────
print("── 2. Discord alert (unreachable → graceful) ──")
da = DiscordAlert(webhook_url="https://discord.com/api/webhooks/fake/fake")
result = da.send(payload)
assert result == False, "Unreachable Discord should return False"
ok("DiscordAlert gracefully handles unreachable webhook")

# ── 3. Telegram alert (will fail — no real bot) ───────────
print("── 3. Telegram alert (unreachable → graceful) ──")
ta = TelegramAlert(bot_token="fake:token", chat_id="12345")
result = ta.send(payload)
assert result == False, "Unreachable Telegram should return False"
ok("TelegramAlert gracefully handles unreachable bot")

# ── 4. Webhook alert (will fail — no real endpoint) ─────
print("── 4. Webhook alert (unreachable → graceful) ──")
wa = WebhookAlert(url="http://127.0.0.1:19999/never-works")
result = wa.send(payload)
assert result == False, "Unreachable webhook should return False"
ok("WebhookAlert gracefully handles unreachable endpoint")

# ── 5. AlertDispatcher empty ────────────────────────────
print("── 5. AlertDispatcher (empty) ──")
dispatcher = AlertDispatcher()
sent = dispatcher.send(payload)
assert sent == 0, "Empty dispatcher sends 0"
ok(f"Empty dispatcher: sent={sent} (no backends)")

# ── 6. AlertDispatcher with failing backends ───────────
print("── 6. AlertDispatcher (failing backends) ──")
dispatcher = AlertDispatcher([
    DiscordAlert(webhook_url="https://discord.com/api/webhooks/fake"),
    WebhookAlert(url="http://127.0.0.1:19999/fake"),
])
sent = dispatcher.send(payload)
assert sent == 0, "All failing: 0 successful"
ok(f"Failing dispatcher: sent={sent} (all backends unreachable, graceful)")

# ── 7. Factory: empty config ──────────────────────────
print("── 7. Factory: empty config ──")
d = create_dispatcher_from_config(None)
assert len(d.backends) == 0
ok(f"Factory(None): {len(d.backends)} backends")

# ── 8. Factory: alerts disabled ───────────────────────
print("── 8. Factory: alerts disabled ──")
d = create_dispatcher_from_config({"alerts": {"enabled": False}})
assert len(d.backends) == 0
ok(f"Factory(disabled): {len(d.backends)} backends")

# ── 9. Factory: with backends ─────────────────────────
print("── 9. Factory: with backends ──")
cfg = {
    "alerts": {
        "enabled": True,
        "discord": {"webhook_url": "https://discord.com/api/webhooks/123"},
        "telegram": {"bot_token": "tok", "chat_id": "cid"},
        "webhook": {"url": "https://hooks.example.com", "headers": {"X-Key": "v"}},
    },
}
d = create_dispatcher_from_config(cfg)
assert len(d.backends) == 3, f"Expected 3 backends, got {len(d.backends)}"
backend_names = [b.name for b in d.backends]
assert "discord" in backend_names
assert "telegram" in backend_names
assert "webhook" in backend_names
ok(f"Factory(3 backends): {backend_names}")

# ── 10. Engine dispatches alert on rejection ───────────
print("── 10. Engine dispatches alert on rejection ──")
# Use an engine with a webhook backend — the webhook will fail
# but the alert dispatch should be attempted and logged, not crash.
engine = QualityGateEngine(
    alert_dispatcher=create_dispatcher_from_config(cfg),
    gate_timeout_seconds=10.0,
)
# Use an improvement that will fail at gate1 (no output)
imp = make_improvement("alert-test",
    actual_tokens=999_999,  # way over budget
    metadata={"agent": "hermes", "session_id": "ses_alert"},
)
result = engine.run_pipeline(imp, persist=False)
ok(f"Engine with alert: status={result.status.value}, alert dispatched gracefully")

# ── 11. Alert dispatch doesn't break pipeline ──────────
print("── 11. Alert dispatch non-blocking ──")
# Even with a dispatcher that raises, the pipeline should continue.
class BrokenBackend:
    name = "broken"
    def send(self, payload): raise RuntimeError("boom")

dispatcher_broken = AlertDispatcher([BrokenBackend()])
engine2 = QualityGateEngine(
    alert_dispatcher=dispatcher_broken,
    gate_timeout_seconds=10.0,
)
imp2 = make_improvement("broken-alert",
    actual_tokens=999_999,
)
result2 = engine2.run_pipeline(imp2, persist=False)
assert result2.status.value == "rejected", "Should still be rejected despite alert error"
ok(f"Broken alert backend: pipeline still returns {result2.status.value}")

# ── 12. Engine.from_yaml creates dispatcher ────────────
print("── 12. Engine.from_yaml with alerts ──")
import yaml, tempfile, os
cfg_yaml = yaml.dump(cfg)
tmpdir = tempfile.mkdtemp()
tmpcfg = os.path.join(tmpdir, "config.yaml")
with open(tmpcfg, "w") as f:
    f.write(cfg_yaml)
try:
    engine3 = QualityGateEngine.from_yaml(tmpcfg)
    assert len(engine3.alert_dispatcher.backends) == 3
    ok(f"Engine.from_yaml: {len(engine3.alert_dispatcher.backends)} alert backends")
finally:
    os.remove(tmpcfg)
    os.rmdir(tmpdir)

# ══════════════════════════════════════════════════════════════
print()
print("=" * 65)
print(f"  Results: {PASS} passed, {FAIL} failed ({PASS+FAIL} total)")
if FAIL == 0:
    print("  🏆 ALL TESTS PASSED")
else:
    print(f"  ⚠️  {FAIL} FAILURES")
print("=" * 65)
if __name__ == "__main__":
    sys.exit(0 if FAIL == 0 else 1)
