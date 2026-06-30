-- 024 — OWNER del bot en BD (PR6.1, BUG-4). El dueño vivía SOLO en un JSON en cwd
-- (telegram_owner.json) → si el archivo no se encontraba (como en la migración a
-- contenedores), is_authorized()=False → "Foresito olvidó todo / no te reconozco".
-- Punto único de fallo frágil. Esta tabla hace la BD la FUENTE DE VERDAD robusta:
-- la BD siempre está montada (es el corazón de For3s) y viaja con los backups/dumps.
-- El JSON queda como caché/compat; si falla, OwnerStore cae a la BD.
--
-- 1 sola fila esperada (single-owner). workspace para multi-tenant futuro.

CREATE TABLE IF NOT EXISTS owner (
    workspace   TEXT PRIMARY KEY DEFAULT 'default',
    owner_id    BIGINT NOT NULL,
    creado_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
