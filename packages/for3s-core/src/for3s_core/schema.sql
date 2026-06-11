-- For3s OS — esquema H2 "RECUERDA" (memoria persistente + audit chain).
-- Event Sourcing: episodes_events es append-only. audit_events es inmutable
-- (hash chain SHA-256 + trigger que bloquea UPDATE/DELETE).
-- Idempotente: se puede correr varias veces sin romper.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── sessions: una fila por conversación ──────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel     TEXT NOT NULL DEFAULT 'cli',
    status      TEXT NOT NULL DEFAULT 'active',
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ── episodes_events: Event Sourcing, cada turno un evento append-only ─
CREATE TABLE IF NOT EXISTS episodes_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

-- ── audit_events: hash chain inmutable (Grafo §6.4) ──────────────────
CREATE TABLE IF NOT EXISTS audit_events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    hash_prev     TEXT NOT NULL,
    hash_self     TEXT NOT NULL
);

-- Índices de consulta
CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes_events (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events (ts);
CREATE INDEX IF NOT EXISTS idx_audit_ws ON audit_events (workspace_id, ts);

-- ── INMUTABILIDAD del audit: bloquear UPDATE y DELETE ────────────────
CREATE OR REPLACE FUNCTION for3s_block_audit_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events es inmutable: % no permitido (append-only)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_immutable ON audit_events;
CREATE TRIGGER trg_audit_immutable
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION for3s_block_audit_mutation();