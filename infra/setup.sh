#!/usr/bin/env bash
# Phase 2: 基础设施就绪检查 + unleash 数据库初始化
# 用法: bash infra/setup.sh
set -euo pipefail

echo "Waiting for PostgreSQL..."
until docker compose exec -T postgres pg_isready -U quality_gates -d quality_gates 2>/dev/null; do
  sleep 2
done
echo "  PostgreSQL ready"

echo "Creating unleash database (if not exists)..."
docker compose exec -T postgres createdb -U quality_gates unleash 2>/dev/null || true
echo "  unleash database OK"

echo "Waiting for Jaeger..."
until curl -sf http://localhost:16686/api/services >/dev/null 2>&1; do
  sleep 2
done
echo "  Jaeger ready"

echo "Waiting for Prometheus..."
until curl -sf http://localhost:9090/-/healthy >/dev/null 2>&1; do
  sleep 2
done
echo "  Prometheus ready"

echo "Waiting for Pushgateway..."
until curl -sf http://localhost:9091/metrics >/dev/null 2>&1; do
  sleep 2
done
echo "  Pushgateway ready"

echo "Waiting for Unleash..."
until curl -sf http://localhost:4242/health >/dev/null 2>&1; do
  sleep 2
done
echo "  Unleash ready"

echo ""
echo "=== All infrastructure ready ==="
echo "  PostgreSQL:  localhost:5432 (user: quality_gates)"
echo "  Prometheus:  http://localhost:9090"
echo "  Pushgateway: http://localhost:9091"
echo "  Jaeger:      http://localhost:16686 (OTLP: 4317)"
echo "  Unleash:     http://localhost:4242"
