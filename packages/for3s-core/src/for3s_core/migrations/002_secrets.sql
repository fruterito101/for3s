-- For3s OS — migración 002 (H4): tabla de SECRETOS CIFRADOS (KEK, R4).
-- Los secretos NUNCA se guardan en texto plano: nonce + ciphertext (AES-256-GCM
-- con clave derivada por workspace vía HKDF de la master key local).
-- Idempotente.

CREATE TABLE IF NOT EXISTS secrets (
    workspace_id TEXT        NOT NULL,
    name         TEXT        NOT NULL,
    nonce        BYTEA       NOT NULL,
    ciphertext   BYTEA       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, name)
);
