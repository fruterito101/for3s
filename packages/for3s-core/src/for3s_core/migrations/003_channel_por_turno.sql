-- 003 — Bug E: trazabilidad de canal POR TURNO (no solo por sesión).
-- Hasta ahora el canal vivía solo en sessions, y por ON CONFLICT DO NOTHING
-- nunca se actualizaba: la sesión "brian" (creada desde CLI) quedaba marcada
-- 'cli' para siempre aunque Telegram la usara. Resultado: imposible saber por
-- qué canal entró cada mensaje.
--
-- Decisión de diseño (el dueño, 2026-06-13): memoria COMPARTIDA entre CLI y
-- Telegram (un solo cerebro, sesión "brian"). Lo que se arregla es la
-- TRAZABILIDAD: cada turno guarda su propio canal de origen.
--
-- Aditiva e idempotente: solo agrega una columna con default. No toca los
-- 54 turnos existentes (quedan con 'cli', que es de donde venían la mayoría).

ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'cli';