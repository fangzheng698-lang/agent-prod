# Changelog

All notable changes to agent-prod will be documented in this file.

## [1.0.0] - 2026-07-01

### Added
- Gate0-Gate7 production quality gate pipeline with readiness, attribution, qclaw watchdog controls, and CI coverage.

### Changed
- Promoted package, SDK, API metadata, and README version to 1.0.0.

## [0.3.0] - 2026-06-23

### Added
- **Real-time Hermes integration**: Session watchdog daemon (`hermes-prod watch`) monitors `~/.hermes/sessions/` via inotify and POSTs execution traces to quality gate pipeline in real time. Zero Hermes source modification required.
- **API authentication**: Bearer token auth middleware (`AGENT_PROD_API_KEY` env var or `api.key` config). All `/v1/*` endpoints protected when enabled.
- **Rate limiting**: Token-bucket rate limiter per endpoint, configurable via `rate_limit.*` in config.yaml.
- **CI/CD configuration**: `.github/workflows/ci.yml` (test + lint), `.pre-commit-config.yaml` (ruff + mypy), `pyproject.toml` tool configs (ruff, mypy, pytest).
- **Health check enhancements**: `/health` now reports watchdog status, rate limit stats, and authentication mode.

### Changed
- `pyproject.toml` version aligned to 0.3.0 with `__init__.py`.
- `pip install -e .` now reflects correct version.
- `compute_baseline()` returns full dict (all 12 fields) on empty records instead of partial dict causing KeyError.

### Fixed
- `test_e2e_flywheel.py`: graceful skip when server not running (no more KeyError on `token_std`).

## [0.2.1] - 2026-06-23

### Added
- `AgentTrace` universal trace format (`trace/models.py`).
- `TraceAdapter` registry with Hermes, ClaudeCode, Codex, Generic adapters (`trace/adapters.py`).
- `POST /v1/agent/evaluate` endpoint — universal quality gate entry point.
- `POST /v1/agent/evaluate/dry-run` — evaluate without persisting.
- Per-agent type thresholds (`gates/thresholds.py`, `config.yaml` `per_agent` sections).
- Alert dispatch system (`gates/alerts.py`) — Discord, Telegram, webhook backends.
- `GET /v1/agent/types` and `GET /v1/agent/thresholds` endpoints.

### Changed
- `Gate3Config` and `Gate4Config` now support `resolve_for_agent()` for per-agent threshold overrides.
- `QualityGateEngine` passes `raw_config` to gates for runtime threshold resolution.

### Fixed
- `cli.py`: fixed `app.main:app` → `agent_prod.server.app:app` for `serve` and `init` commands.

## [0.2.0] - 2026-06-22

### Added
- Initial skeleton release with layered package structure.
- `GatePlugin` ABC — public API contract for quality gates.
- 5 built-in gates: execution, trace integrity, regression, gray release, release audit.
- `QualityGateEngine` pipeline with pluggable gate registration.
- `AgentRuntime` with LLM client, tool registry, and budget control.
- FastAPI server (`/v1/chat/completions`, `/health`, `/metrics`, session CRUD).
- Causal attribution engine: OLS, Granger causality, counterfactual baseline, Bonferroni correction.
- Data flywheel: statistical baseline, EWMA, trend detection, Welch t-test.
- Adaptive gates: dynamic thresholds from real execution data.
- Stuctured logging (structlog) and embedded Prometheus metrics.
- `pip install agent-prod` installable package.
- Session ingestion from Hermes session files.
- Docker Compose for full deployment (Postgres, Prometheus, Jaeger, Unleash).
- Testing suite: benchmark, replay, profiling, stress testing, governance.
