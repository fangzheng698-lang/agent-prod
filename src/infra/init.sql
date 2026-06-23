-- Phase 2: PostgreSQL 初始化
-- docker-compose 首次启动时自动执行
-- unleash 数据库需要在容器启动后手动创建:
--   docker compose exec -T postgres createdb -U quality_gates unleash

CREATE TABLE IF NOT EXISTS improvements (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_improvements_status ON improvements(status);
CREATE INDEX IF NOT EXISTS idx_improvements_created_at ON improvements(created_at);
