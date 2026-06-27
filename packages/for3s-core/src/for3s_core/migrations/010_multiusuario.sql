-- 010 — Multi-usuario por agente (H8 S10, 2026-06-23).
--
-- Hoy For3s es single-owner (1 persona). Esto añade el concepto de EQUIPO: varias
-- personas compartiendo un mismo agente, con roles + un encargado. ADITIVA y SEGURA:
-- el modo single-owner sigue funcionando (si no hay equipo configurado, todo opera
-- como hoy). No toca tablas existentes.
--
-- Modelo "PUERTA" (decisión de diseño): el ingreso al equipo se controla con un flag
-- puerta_abierta. Abierta → quien escriba al bot entra; cerrada → solo los de adentro.
-- Sacar/denegar miembros se diseña MÁS ADELANTE (agente colaborativo).

-- 1) equipos: un espacio compartido. Hoy habrá 1 (el del dueño), extensible a N.
CREATE TABLE IF NOT EXISTS equipos (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre        TEXT NOT NULL,
    encargado_id  BIGINT NOT NULL,          -- user_id (Telegram) del encargado/dueño
    puerta_abierta BOOLEAN NOT NULL DEFAULT false,  -- modelo puerta (default cerrada)
    creado_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2) miembros: qué personas pertenecen a un equipo y con qué rol.
CREATE TABLE IF NOT EXISTS equipo_miembros (
    equipo_id   BIGINT NOT NULL REFERENCES equipos(id),
    user_id     BIGINT NOT NULL,            -- user_id de Telegram
    rol         TEXT NOT NULL DEFAULT 'miembro',  -- 'encargado' | 'miembro'
    nombre      TEXT,                        -- nombre legible (para mostrar)
    entro_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    activo      BOOLEAN NOT NULL DEFAULT true,    -- soft-remove futuro (kick = false)
    PRIMARY KEY (equipo_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_miembros_user ON equipo_miembros (user_id) WHERE activo;
