# Dockerfile for agent-prod
# Multi-stage build: produces a small production image
# Build: docker build -t agent-prod .
# Run:   docker compose up -d

FROM python:3.11-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ src/

# Build wheel with all extras
RUN pip install build --no-cache-dir \
    && python -m build --wheel \
    && pip install --no-cache-dir dist/agent_prod-*.whl[mcp,postgres] \
    && rm -rf dist build

# ── Runtime ──────────────────────────────────────────────
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash agent-prod \
    && mkdir -p /app/data \
    && chown agent-prod:agent-prod /app/data

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages/ /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/local/bin/agent-prod* /usr/local/bin/
COPY pyproject.toml README.md ./
COPY src/ src/

ENV QUALITY_GATES_MODE=production
ENV QUALITY_GATES_ENABLED=true

USER agent-prod
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["agent-prod", "serve", "--host", "0.0.0.0"]