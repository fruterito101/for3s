-- 016 — STATUS por hilo (AI4 auto-retomar, 2026-06-23).
--
-- Cada hilo (persona×tema, del #6/AI2) tiene un STATUS corto curado: en qué quedamos,
-- fase, próximo paso. Se REGENERA de noche (junto con H6/CLS, cero costo de día)
-- y se INYECTA al contexto cuando la persona retoma ese hilo tras inactividad
-- (>~3h). Es el "RETOMAR.md" automático por conversación.
--
-- ADITIVA: tabla nueva. session_id es PK (un STATUS por hilo). Defensivo en el
-- código: si falta o falla, el flujo normal (12 turnos + memoria) sigue igual.

CREATE TABLE IF NOT EXISTS hilo_status (
    session_id      TEXT PRIMARY KEY,         -- hilo (persona×tema)
    texto           TEXT NOT NULL,            -- STATUS corto curado (≤~6 líneas)
    actualizado_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
