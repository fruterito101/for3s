-- 019 — Skills (H10 "APRENDE", 2026-06-24).
--
-- For3s puede TENER y USAR skills: recetas reutilizables (SKILL.md = cuándo usarla
-- + pasos) que el agente aplica con sus tools. H10 = almacenamiento + uso manual.
-- DB-backed (estilo For3s: consistente, transaccional, respaldado por backup noche).
--
-- Campos pensados para TODO el hito (no re-migrar):
--   lifecycle  → para curación nocturna (H11/H12): active→stale→archived (recuperable)
--   provenance → 'usuario' (la pidió un humano, intocable) | 'auto' (auto-generada,
--                el governor/curator SÍ la gestiona). Clave de seguridad (H11).
--   veces_usada/ultimo_uso → para que la curación archive las que no se usan.
--   creada_por → telegram_user_id de quién la originó (multi-usuario).

CREATE TABLE IF NOT EXISTS skills (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre        TEXT NOT NULL,            -- slug, único por categoría
    categoria     TEXT NOT NULL DEFAULT 'general',
    descripcion   TEXT NOT NULL DEFAULT '',  -- una línea (para el listado)
    contenido     TEXT NOT NULL,            -- el SKILL.md completo
    tags          JSONB NOT NULL DEFAULT '[]'::jsonb,
    version       TEXT NOT NULL DEFAULT '0.1.0',
    lifecycle     TEXT NOT NULL DEFAULT 'active',   -- active|stale|archived
    provenance    TEXT NOT NULL DEFAULT 'usuario',  -- usuario|auto
    pinned        BOOLEAN NOT NULL DEFAULT false,   -- pinned se salta la curación
    creada_por    BIGINT,                   -- telegram_user_id (NULL = sistema/CLI)
    veces_usada   INT NOT NULL DEFAULT 0,
    ultimo_uso    TIMESTAMPTZ,
    creada_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizada_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (categoria, nombre)
);

CREATE INDEX IF NOT EXISTS idx_skills_activas
    ON skills (lifecycle) WHERE lifecycle = 'active';
CREATE INDEX IF NOT EXISTS idx_skills_provenance
    ON skills (provenance, lifecycle);
