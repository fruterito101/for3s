-- 014 — Temas por persona (AI2, 2026-06-23).
--
-- Un mismo chat de Telegram (una persona) puede tener VARIOS temas/hilos
-- separados (ej. "backend", "lista-del-dia"), cada uno con su propia
-- conversación. Solo UNO está activo a la vez por persona. El conocimiento
-- (grafo/CLS) se sigue compartiendo; lo que se separa es el HILO.
--
-- El session_id de cada turno pasa a ser "tg:<user_id>:<tema>" (extiende el
-- #6, que era "tg:<user_id>"). El tema por defecto es "general" → opt-in: sin
-- usar /tema, todo va a "general" (= como hoy). ADITIVA y compat.

CREATE TABLE IF NOT EXISTS temas (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL,           -- dueño del tema (telegram user_id)
    nombre      TEXT NOT NULL,             -- slug del tema (ej. "backend")
    activo      BOOLEAN NOT NULL DEFAULT false,  -- ¿es el tema activo de esta persona?
    creado_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_uso  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, nombre)
);

-- buscar rápido el tema activo de una persona
CREATE INDEX IF NOT EXISTS idx_temas_activo
    ON temas (user_id) WHERE activo;