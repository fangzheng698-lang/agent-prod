# ── Phase 8.3: agent-prod 生产镜像 ────────────────────────────
# 多阶段构建：builder 阶段安装依赖，最终阶段仅包含运行时。
#
# 构建:  docker build -t agent-prod:latest -f docker/Dockerfile .
# 运行:  docker compose up -d
#
# 特点:
#   - 基于 python:3.11-slim，轻量级
#   - 多阶段构建，减小镜像体积
#   - 非 root 用户运行
#   - 健康检查内置
#   - 支持环境变量配置

# ── Stage 1: Builder ────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 安装构建依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ────────────────────────────────────────
FROM python:3.11-slim AS runtime

# 安全: 创建非 root 用户
RUN groupadd -r agent && useradd -r -g agent -d /app -s /sbin/nologin agent

# 安装运行时系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 复制已安装的 Python 包
COPY --from=builder /install /usr/local

# 复制应用源码
COPY requirements.txt .
COPY app/ ./app/
COPY tools_impl/ ./tools_impl/
COPY quality_gates/ ./quality_gates/
COPY skills/ ./skills/
COPY infra/ ./infra/

# 创建数据目录并设置权限
RUN mkdir -p /app/data /app/workspace && \
    chown -R agent:agent /app

# 切换到非 root 用户
USER agent

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

EXPOSE 8000

# 默认环境变量（可在 docker-compose 中覆盖）
ENV HOST=0.0.0.0
ENV PORT=8000
ENV DATABASE_URL=sqlite+aiosqlite:///./data/agent.db
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
