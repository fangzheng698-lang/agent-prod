DROP TABLE IF EXISTS improvements;

CREATE TABLE improvements (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_improvements_status ON improvements(status);
CREATE INDEX idx_improvements_created_at ON improvements(created_at);