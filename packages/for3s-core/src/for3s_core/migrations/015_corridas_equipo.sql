-- 015 — Audit trail del equipo multi-agente (AI3 handoff DB-backed, 2026-06-23).
--
-- Audit trail DB-backed del equipo. Cada corrida del equipo multi-agente (H8) queda
-- REGISTRADA: qué se pidió y qué devolvió CADA specialist. Antes se perdía en RAM.
--
-- Principio de SEPARACIÓN DE ESCRITURA: el registro lo escribe el HUB/coordinador,
-- no los specialists. Semilla de la separación de roles del gate (AI3 p2/E).
--
-- ADITIVA: tablas nuevas, no toca nada existente. Defensiva en el código (si el
-- registro falla, NO rompe la entrega del informe).

-- 1) la corrida: una fila por cada vez que se lanza el equipo.
CREATE TABLE IF NOT EXISTS corridas_equipo (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id       TEXT NOT NULL,            -- hilo (persona×tema) que la disparó
    telegram_user_id BIGINT,                   -- autor (#6); NULL = CLI/legado
    tarea            TEXT NOT NULL,            -- la petición original
    familia          TEXT NOT NULL,            -- 'tecnica' | 'general'
    n_specialists    INT NOT NULL DEFAULT 0,
    n_ok             INT NOT NULL DEFAULT 0,
    segundos         REAL NOT NULL DEFAULT 0,
    tokens_in        INT NOT NULL DEFAULT 0,
    tokens_out       INT NOT NULL DEFAULT 0,
    informe          TEXT,                     -- la síntesis final entregada
    creado_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_corridas_sesion
    ON corridas_equipo (session_id, creado_at DESC);

-- 2) el reporte de cada specialist dentro de una corrida (texto completo).
CREATE TABLE IF NOT EXISTS corrida_reportes (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    corrida_id  BIGINT NOT NULL REFERENCES corridas_equipo(id) ON DELETE CASCADE,
    specialist  TEXT NOT NULL,                 -- nombre del specialist
    ok          BOOLEAN NOT NULL,
    tokens_in   INT NOT NULL DEFAULT 0,
    tokens_out  INT NOT NULL DEFAULT 0,
    segundos    REAL NOT NULL DEFAULT 0,
    texto       TEXT NOT NULL                  -- el análisis íntegro (o motivo si falló)
);

CREATE INDEX IF NOT EXISTS idx_corrida_reportes_corrida
    ON corrida_reportes (corrida_id);