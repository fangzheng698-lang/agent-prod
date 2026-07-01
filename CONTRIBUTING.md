# Contributing to agent-prod

Thank you for helping improve agent-prod. This project welcomes bug reports,
documentation fixes, tests, and focused pull requests.

## Development Setup

```bash
git clone https://github.com/fangzheng698-lang/agent-prod.git
cd agent-prod
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

## Run Checks

Before opening a pull request, run:

```bash
PYTHONPATH=src python -m pytest -q
python -m ruff check \
  src/agent_prod/gates/attribution.py \
  src/agent_prod/integration/qclaw_watchdog.py \
  tests/test_attribution.py \
  tests/test_qclaw_watchdog.py \
  --ignore RUF001,RUF002,RUF003
```

## Pull Request Guidelines

- Keep changes focused on one problem or feature.
- Add or update tests when behavior changes.
- Update README, CHANGELOG, or docs when public behavior changes.
- Do not commit secrets, API keys, local absolute paths, or private data.
- Explain why the change is needed and how it was verified.

## Issue Guidelines

For bugs, include:

- agent-prod version or commit SHA
- Python version
- operating system
- steps to reproduce
- expected behavior
- actual behavior and logs

For feature requests, describe the use case and the smallest useful behavior.

## Code Style

Follow the existing code style in nearby files. Prefer small, readable changes
over broad refactors. Keep public APIs backward compatible unless a breaking
change is clearly justified.
