"""Tests for AgentRunner and gate_stress."""
import pytest
from agent_prod.testing.benchmark import (
    BenchmarkRunner, BenchmarkSnapshot, AgentRunner, compare_snapshots, save_snapshot,
)
from agent_prod.testing.gate_stress import GateStressRunner, StressReport, StressSample


# ════════════════════════════════════════════════════════════
# AgentRunner tests
# ════════════════════════════════════════════════════════════

class MockAgentRunner(AgentRunner):
    """Mock agent that returns predictable metrics."""
    def __init__(self, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()

    async def run(self, prompt: str) -> dict:
        if prompt in self.fail_on:
            raise RuntimeError("mock agent failure")
        return {
            "turns": 2,
            "tokens": 100,
            "gate_pass": True,
            "response": f"mock response to: {prompt[:20]}",
        }


@pytest.mark.asyncio
async def test_agent_runner_protocol():
    runner = MockAgentRunner()
    result = await runner.run("test")
    assert result["turns"] == 2
    assert result["tokens"] == 100
    assert result["gate_pass"] is True


@pytest.mark.asyncio
async def test_benchmark_agent_runner_empty_prompts():
    bm = BenchmarkRunner()
    agent = MockAgentRunner()
    snap = await bm.run_agent_benchmark([], agent)
    assert snap.prompt_count == 0


@pytest.mark.asyncio
async def test_benchmark_agent_runner_single():
    bm = BenchmarkRunner()
    agent = MockAgentRunner()
    snap = await bm.run_agent_benchmark(["hello"], agent)
    assert snap.prompt_count == 1
    assert snap.avg_turns == 2.0
    assert snap.gate_pass_rate == 1.0


@pytest.mark.asyncio
async def test_benchmark_agent_runner_multiple():
    bm = BenchmarkRunner()
    agent = MockAgentRunner()
    prompts = ["a", "b", "c"]
    snap = await bm.run_agent_benchmark(prompts, agent)
    assert snap.prompt_count == 3
    assert snap.avg_turns == 2.0
    assert snap.avg_tokens == 100.0
    assert snap.gate_pass_rate == 1.0


@pytest.mark.asyncio
async def test_benchmark_agent_runner_handles_failures():
    bm = BenchmarkRunner()
    agent = MockAgentRunner(fail_on={"bad"})
    prompts = ["good", "bad", "good"]
    snap = await bm.run_agent_benchmark(prompts, agent)
    assert snap.prompt_count == 3
    assert snap.gate_pass_rate == pytest.approx(2 / 3, abs=0.001)


@pytest.mark.asyncio
async def test_benchmark_default_prompts_exist():
    bm = BenchmarkRunner()
    assert len(bm.DEFAULT_PROMPTS) == 5
    assert all(isinstance(p, str) for p in bm.DEFAULT_PROMPTS)


# ════════════════════════════════════════════════════════════
# GateStress tests
# ════════════════════════════════════════════════════════════

@pytest.fixture
def mock_gateway():
    """Mock gateway that always passes."""
    class MockGateway:
        async def validate(self, session_id, messages, turns):
            return None, True
    return MockGateway()


@pytest.fixture
def mock_turns():
    """Generate fake turn lists."""
    return [[], [], []]  # 3 empty turn batches


@pytest.mark.asyncio
async def test_gate_stress_empty_turns(mock_gateway):
    runner = GateStressRunner()
    report = await runner.stress_test(mock_gateway, [])
    assert report.total_samples == 0


@pytest.mark.asyncio
async def test_gate_stress_basic(mock_gateway, mock_turns):
    runner = GateStressRunner(max_concurrency=2, stability_max_stddev_pct=500.0)
    report = await runner.stress_test(mock_gateway, mock_turns)
    assert report.total_samples == 3
    assert report.passed == 3
    assert report.failed == 0
    assert report.stable is True
    assert report.pass_rate == 1.0


@pytest.mark.asyncio
async def test_gate_stress_report_to_dict(mock_gateway, mock_turns):
    runner = GateStressRunner(stability_max_stddev_pct=500.0)
    report = await runner.stress_test(mock_gateway, mock_turns)
    d = report.to_dict()
    assert d["total_samples"] == 3
    assert d["stable"] is True
    assert "avg_duration_ms" in d


@pytest.mark.asyncio
async def test_gate_stress_report_summary(mock_gateway, mock_turns):
    runner = GateStressRunner()
    report = await runner.stress_test(mock_gateway, mock_turns)
    s = report.summary
    assert "GateStress" in s
    assert "Stable" in s


@pytest.mark.asyncio
async def test_gate_stress_with_errors():
    class FailingGateway:
        called = 0

        async def validate(self, session_id, messages, turns):
            self.called += 1
            if self.called == 2:
                raise RuntimeError("gateway down")
            return None, True

    runner = GateStressRunner(max_concurrency=1)
    report = await runner.stress_test(FailingGateway(), [[], [], []])
    assert report.failed > 0
    assert report.stable is False


@pytest.mark.asyncio
async def test_gate_stress_with_gate_failures():
    class StrictGateway:
        async def validate(self, session_id, messages, turns):
            if len(turns) == 0:
                return None, False  # gate fails on empty turns
            return None, True

    runner = GateStressRunner(max_concurrency=1)
    report = await runner.stress_test(StrictGateway(), [[], []])
    assert report.passed == 0
    assert report.errored > 0
    assert report.stable is False


@pytest.mark.asyncio
async def test_gate_stress_last_report(mock_gateway, mock_turns):
    runner = GateStressRunner()
    assert runner.last_report is None
    await runner.stress_test(mock_gateway, mock_turns)
    assert runner.last_report is not None
    assert runner.last_report.total_samples == 3


@pytest.mark.asyncio
async def test_gate_stress_with_lb_no_ramp(mock_gateway):
    runner = GateStressRunner(max_concurrency=2)
    turns = [[], []]
    report = await runner.stress_test_with_lb(mock_gateway, turns, ramp_up=False)
    assert report.total_samples == 2
    assert report.passed == 2


@pytest.mark.asyncio
async def test_stress_sample_fields():
    s = StressSample(session_id="s-1", gate_pass=True, duration_ms=100.0)
    assert s.session_id == "s-1"
    assert s.gate_pass is True
    assert s.duration_ms == 100.0
    assert s.error == ""
