# Dockerfile for agent-prod
# Multi-stage build: produces a small production image
# Build: docker build -t agent-prod .
# Run:   docker run -p 8000:8000 -e QUALITY_GATES_MODE=memory agent-prod

FROM python:3.11-slim-bookworm

RUN useradd --create-home --shell /bin/bash agent-prod \
    && mkdir -p /app/data \
    && chown agent-prod:agent-prod /app/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/

# Non-editable install: package goes into site-packages
RUN pip install --no-cache-dir .

ENV QUALITY_GATES_MODE=memory
ENV QUALITY_GATES_ENABLED=true

USER agent-prod
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["agent-prod", "serve", "--host", "0.0.0.0"]