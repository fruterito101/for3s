-- 013 — Hilo por usuario (H8 pulido, bug #6, 2026-06-23).
--
-- AUDITORÍA reveló: TODO caía en la sesión 'brian' (589 turnos) y no había forma
-- de saber QUIÉN mandó cada turno. Esto añade la traza del autor.
--
-- A partir de ahora cada persona tiene su PROPIA sesión (session_id derivado de su
-- user_id de Telegram, ej. "tg:<id>"). Esta columna guarda ADEMÁS el user_id crudo
-- de quién mandó el turno, para poder responder "¿quién escribió esto?" con certeza
-- (#3) aunque el session_id cambie de formato en el futuro.
--
-- ADITIVA: columna NULL-able. Los 589 turnos viejos quedan con NULL (eran del dueño
-- en modo single-owner) — se siguen leyendo igual.

ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_episodes_tg_user
    ON episodes_events (telegram_user_id) WHERE telegram_user_id IS NOT NULL;