#!/usr/bin/env bash
# agent-prod 即开即用安装脚本
# 用途: 在新机器上一键安装并启动 agent-prod
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${AGENT_PROD_VENV:-$HERE/.venv}"

echo "=== agent-prod setup ==="

# 1. Python 版本检查
python3 --version 2>&1 | head -1
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    echo "ERROR: Python 3.11+ required"
    exit 1
fi

# 2. 创建虚拟环境（如果不存在）
if [ ! -d "$VENV" ]; then
    echo "→ Creating virtualenv: $VENV"
    python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"

# 3. 安装依赖
echo "→ Installing dependencies..."
pip install --upgrade pip -q
pip install -e "$HERE" -q

# 4. 创建运行时目录
sudo mkdir -p /var/lib/quality_gates /var/log/quality_gates 2>/dev/null || {
    mkdir -p "$HERE/data/quality_gates" "$HERE/data/logs"
    echo "→ Using local data dirs (no sudo)"
}

# 5. 生成 .env（如果不存在）
if [ ! -f "$HERE/.env" ]; then
    cp "$HERE/.env.example" "$HERE/.env" 2>/dev/null || touch "$HERE/.env"
    echo "→ Created .env from example — edit it to set API keys"
fi

# 6. 验证安装
echo "→ Verifying..."
agent-prod --version 2>/dev/null || echo "  (version info may vary)"
agent-prod doctor 2>/dev/null || true

echo ""
echo "=== Setup complete ==="
echo ""
echo "Quick start:"
echo "  cd $HERE && source .venv/bin/activate"
echo "  agent-prod serve --port 8765"
echo "  curl http://localhost:8765/health"
echo ""
echo "Or with docker:"
echo "  docker-compose up -d"