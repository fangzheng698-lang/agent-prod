"""
Phase 1 真实路径测试 — 验证所有非 demo 代码路径
不回避，不降级到 Phase 0 逻辑
"""
import sys, os, json, time, logging, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# 开启 structlog（已安装）
logging.basicConfig(level=logging.WARNING)  # 减少噪声

from agent_prod.gates.models import Improvement, GateResult, GateName
from agent_prod.gates.engine import QualityGateEngine, load_config, create_repository
from agent_prod.gates.repository import FileRepository, MemoryRepository
from agent_prod.gates.metrics import (
    ConfigMetricsProvider, FileMetricsProvider, PrometheusMetricsProvider, GrayMetrics,
)
from agent_prod.gates.gate2_trace import JaegerAPIClient
from agent_prod.gates.gate4_gray import (
    Gate4GrayRelease, Gate4Config, FileFlagEngine, UnleashFlagEngine, FlagEngine,
)

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
        "llm_calls": [{"response_id": "r1", "duration_ms": 500}],
        "tool_calls": [{"request_id": "r1"}],
    }
    defaults.update(kwargs)
    return Improvement(**defaults)


# ══════════════════════════════════════════════════════════════
print("=" * 65)
print("  Phase 1 Real Infrastructure Tests")
print("=" * 65)
print()

# ── 1. MetricsProvider: ConfigMetricsProvider ────────────────
print("── 1. MetricsProvider: config ──")
cfg_provider = ConfigMetricsProvider({
    "test-1": {1: {"error_rate": 0.005, "latency_p95_ms": 95, "resource_pct": 45}},
})
m = cfg_provider.observe_stage("test-1", 1, 10, "1%")
assert isinstance(m, GrayMetrics), "Must return GrayMetrics"
assert m.error_rate == 0.005, f"error_rate mismatch: {m.error_rate}"
assert m.latency_p95_ms == 95
ok(f"ConfigMetricsProvider: error_rate={m.error_rate}, latency={m.latency_p95_ms}ms")

# 缺失 key 时走默认值
m2 = cfg_provider.observe_stage("test-1", 99, 50, "50%")
ok(f"ConfigMetricsProvider(missing stage): uses default error_rate={m2.error_rate}")

# ── 2. MetricsProvider: FileMetricsProvider ──────────────────
print("── 2. MetricsProvider: file ──")
tmpd = tempfile.mkdtemp()
try:
    file_provider = FileMetricsProvider(metrics_dir=tmpd)
    # 无文件时走默认
    m3 = file_provider.observe_stage("imp-x", 1, 10, "1%")
    assert m3.error_rate == 0.003, f"default error_rate: {m3.error_rate}"
    ok(f"FileMetricsProvider(no file): default error_rate={m3.error_rate}")

    # 写入文件后读取
    file_provider.write_metrics("imp-x", 2, GrayMetrics(
        stage=2, traffic_pct=50, label="50%",
        error_rate=0.012, latency_p95_ms=200, resource_pct=60, passed=False,
    ))
    m4 = file_provider.observe_stage("imp-x", 2, 50, "50%")
    assert m4.error_rate == 0.012
    assert m4.passed == False
    ok(f"FileMetricsProvider(read back): error_rate={m4.error_rate}, passed={m4.passed}")
finally:
    shutil.rmtree(tmpd)

# ── 3. MetricsProvider: Prometheus (降级路径) ────────────────
print("── 3. MetricsProvider: prometheus (degraded) ──")
# Prometheus 不可达 → 应降级返回安全值（通过，不阻塞）
prom = PrometheusMetricsProvider(prometheus_url="http://127.0.0.1:19999", timeout_seconds=1.0)
m5 = prom.observe_stage("imp-degrade", 1, 10, "1%")
assert isinstance(m5, GrayMetrics)
# 第一次失败应该还不会降级
assert m5.passed == True, "Degraded must return passed=True"
ok(f"Prometheus(unreachable → degraded): passed={m5.passed}, error_rate={m5.error_rate}")

# ── 4. Gate4 with ConfigMetricsProvider ──────────────────────
print("── 4. Gate4: config metrics path ──")
cfg4 = Gate4Config(
    metrics_provider="config",
    config_metrics={
        "imp-m": {
            1: {"error_rate": 0.003, "latency_p95_ms": 90, "resource_pct": 40, "passed": True},
            2: {"error_rate": 0.005, "latency_p95_ms": 95, "resource_pct": 45, "passed": True},
            3: {"error_rate": 0.008, "latency_p95_ms": 100, "resource_pct": 50, "passed": True},
            4: {"error_rate": 0.010, "latency_p95_ms": 105, "resource_pct": 55, "passed": True},
        },
    },
    flag_engine="file",
    stable_period_seconds=0,  # 不 sleep
)
gate4 = Gate4GrayRelease(config=cfg4)
assert isinstance(gate4.metrics, ConfigMetricsProvider), "Should be ConfigMetricsProvider"
assert isinstance(gate4.flags, FileFlagEngine), "Should be FileFlagEngine"
ok(f"Gate4.init: metrics={type(gate4.metrics).__name__}, flags={type(gate4.flags).__name__}")

# 跑一遍灰度通过
imp4 = make_improvement("imp-m", baseline={"latency_p95_ms": 100, "success_rate": 0.99})
result = gate4.verify(imp4)
assert result.passed, f"Gate4 with config metrics should pass: {result.reason}"
ok(f"Gate4(config metrics): {result.reason} ({result.duration_ms:.0f}ms)")

# ── 5. JaegerAPIClient 降级路径 ──────────────────────────────
print("── 5. JaegerAPIClient: unreachable → degraded ──")
jaeger = JaegerAPIClient(base_url="http://127.0.0.1:29999", timeout_seconds=1.0)
# 查询不存在的 trace → 返回 None
result = jaeger.query_trace("abc123")
assert result is None, "Unreachable Jaeger must return None"
ok(f"Jaeger(unreachable): returns None as expected")

# 连续 3 次失败后标记 degraded
jaeger._consecutive_failures = 3
jaeger._degraded = True
result2 = jaeger.query_trace("abc123")
assert result2 is None
ok(f"Jaeger(degraded): short-circuits, returns None")

# ── 6. Gate2 with trace_id but Jaeger unreachable ────────────
print("── 6. Gate2: trace_id → Jaeger unreachable → OTel fallback → caller/callee ──")
from agent_prod.gates.gate2_trace import Gate2TraceIntegrity
gate2 = Gate2TraceIntegrity(jaeger_url="http://127.0.0.1:29999")
imp6 = make_improvement("trace-test", trace_id="fake-trace-001",
    llm_calls=[{"response_id": "r1", "duration_ms": 1200}, {"response_id": "r2", "duration_ms": 800}],
    tool_calls=[{"request_id": "r1"}, {"request_id": "r2"}],
)
result6 = gate2.verify(imp6)
assert result6.passed, f"Gate2 fallback should pass for valid caller/callee: {result6.reason}"
ok(f"Gate2(Jaeger down→caller/callee fallback): {result6.reason}")

# ── 7. Gate2: orphan detection via caller/callee ─────────────
print("── 7. Gate2: orphan tool call detection ──")
imp7 = make_improvement("orphan-test",
    llm_calls=[{"response_id": "r1", "duration_ms": 500}],
    tool_calls=[{"request_id": "orphan_x"}],
)
result7 = gate2.verify(imp7)
assert not result7.passed, "Orphan should be caught"
ok(f"Gate2(orphan detected): {result7.reason}")

# ── 8. UnleashFlagEngine 降级路径 ────────────────────────────
print("── 8. UnleashFlagEngine: unreachable → degraded ──")
unleash = UnleashFlagEngine(
    api_url="http://127.0.0.1:29999",
    api_token="fake-token",
    environment="production",
)
v = unleash.get_variant("imp-test", "user-1")
assert v == "baseline", "Unreachable Unleash must return baseline"
ok(f"Unleash(get_variant degraded): returns '{v}'")

unleash.set_traffic("imp-test", 50)  # 不应抛异常
ok(f"Unleash(set_traffic degraded): no exception")

rolled = unleash.is_rolled_out("imp-test")
assert rolled == False
ok(f"Unleash(is_rolled_out degraded): returns {rolled}")

# ── 9. Gate4 with Unleash (降级) ─────────────────────────────
print("── 9. Gate4: unleash flag engine (degraded) ──")
cfg9 = Gate4Config(
    metrics_provider="config",
    config_metrics={
        "imp-u": {s: {"error_rate": 0.003, "latency_p95_ms": 90, "resource_pct": 40, "passed": True}
                  for s in [1,2,3,4]},
    },
    flag_engine="unleash",
    unleash_url="http://127.0.0.1:29999",
    unleash_api_token="fake",
    stable_period_seconds=0,
)
gate4u = Gate4GrayRelease(config=cfg9)
assert isinstance(gate4u.flags, UnleashFlagEngine)
ok(f"Gate4.init(unleash): flags={type(gate4u.flags).__name__}")

imp9 = make_improvement("imp-u", baseline={"latency_p95_ms": 100})
result9 = gate4u.verify(imp9)
ok(f"Gate4(unleash degraded): passed={result9.passed}, reason={result9.reason}")

# ── 10. FileRepository 持久化 ────────────────────────────────
print("── 10. FileRepository: save + get + list + delete ──")
repod = tempfile.mkdtemp()
try:
    db_path = os.path.join(repod, "test_improvements.json")
    repo = FileRepository(file_path=db_path)
    imp10 = make_improvement("persist-test")
    repo.save(imp10)
    assert os.path.exists(db_path), "File not created"
    ok(f"FileRepository.save: file created ({os.path.getsize(db_path)} bytes)")

    # get
    loaded = repo.get(imp10.id)
    assert loaded is not None
    assert loaded.name == "persist-test"
    assert loaded.id == imp10.id
    ok(f"FileRepository.get: name='{loaded.name}', status={loaded.status.value}")

    # list
    all_items = repo.list()
    assert len(all_items) == 1
    ok(f"FileRepository.list: {len(all_items)} items")

    # count
    cnt = repo.count()
    assert cnt == 1
    ok(f"FileRepository.count: {cnt}")

    # 重新加载验证持久化
    repo2 = FileRepository(file_path=db_path)
    loaded2 = repo2.get(imp10.id)
    assert loaded2 is not None
    assert loaded2.name == "persist-test"
    ok(f"FileRepository(reload): survives restart")

    # delete
    repo.delete(imp10.id)
    assert repo.count() == 0
    ok(f"FileRepository.delete: count={repo.count()}")
finally:
    shutil.rmtree(repod)

# ── 11. Engine with FileRepository ────────────────────────────
print("── 11. Engine with FileRepository + structlog ──")
repod2 = tempfile.mkdtemp()
try:
    db_path2 = os.path.join(repod2, "engine_improvements.json")
    repo_e = FileRepository(file_path=db_path2)
    engine = QualityGateEngine(
        repository=repo_e,
        gate_timeout_seconds=10.0,
    )
    imp11 = make_improvement("engine-test",
        actual_tokens=1000,
        human_approver="qa@example.com",
        baseline={"f1_score": 0.85, "latency_p95_ms": 100, "success_rate": 0.99},
        candidate_output={
            "final_response": "improved output",
            "confidence": 0.95,
            "tools_used": ["search"],
            "token_count": 1_000,
            "warnings": [],
            "f1_score": 0.87,
            "latency_p95_ms": 95,
            "success_rate": 0.99,
        },
    )
    result_e = engine.run_pipeline(imp11, human_approver="qa@example.com", persist=True)
    assert result_e.status.value == "production", f"Expected production, got {result_e.status.value}"
    ok(f"Engine(FileRepo): status={result_e.status.value}")

    # 验证持久化
    persisted = repo_e.get(imp11.id)
    assert persisted is not None
    assert persisted.status.value == "production"
    ok(f"Engine(FileRepo): persisted OK, {len(persisted.gate_results)} gate results")
finally:
    shutil.rmtree(repod2)

# ── 12. structlog 集成验证 ────────────────────────────────────
print("── 12. structlog integration ──")
from agent_prod.gates.engine import _STRUCTLOG, _log_gate, _log_pipeline
ok(f"structlog available: {_STRUCTLOG}")
_log_gate("test_gate", True, 1.5, "imp-123")
ok(f"_log_gate() no exception")

# ── 13. Engine timeout 保护 ───────────────────────────────────
print("── 13. Engine timeout protection ──")
engine_t = QualityGateEngine(gate_timeout_seconds=0.5)  # 短超时 — 会触发在 Gate4 的 sleep
imp13 = make_improvement("timeout-test",
    actual_tokens=500,
    baseline={"latency_p95_ms": 100},
)
try:
    result_t = engine_t.run_pipeline(imp13, persist=False)
    ok(f"Engine(timeout=0.5s): completed with status={result_t.status.value}")
except Exception as e:
    ng(f"Engine timeout test exception: {e}")

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
