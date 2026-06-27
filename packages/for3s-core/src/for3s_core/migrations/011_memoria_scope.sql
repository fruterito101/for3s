-- 011 — Memoria con SCOPE híbrido para multi-usuario (H8 S10c, 2026-06-23).
--
-- Hoy la memoria (episodes_events) es de UNA persona (sesión "brian"). En equipo
-- queremos memoria HÍBRIDA:
--   🔒 PRIVADA  → de quien la dijo; solo esa persona la recupera.
--   🟢 COMÚN    → del equipo; todos sus miembros la recuperan.
--
-- Default elegido por Nota: PRIVADO. Un recuerdo nace privado de quien habló;
-- lo común se marca explícito (equipo_id no nulo).
--
-- ADITIVA y COMPAT: ambas columnas son NULL-able. Los recuerdos viejos (NULL en
-- las dos) = del dueño en modo single-owner → se siguen recuperando igual.

ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS owner_user_id BIGINT,   -- dueño del recuerdo (privado)
    ADD COLUMN IF NOT EXISTS equipo_id     BIGINT;   -- si no es NULL → común del equipo

-- índice para filtrar rápido por persona en búsquedas semánticas
CREATE INDEX IF NOT EXISTS idx_episodes_owner
    ON episodes_events (owner_user_id) WHERE owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_episodes_equipo
    ON episodes_events (equipo_id) WHERE equipo_id IS NOT NULL;
