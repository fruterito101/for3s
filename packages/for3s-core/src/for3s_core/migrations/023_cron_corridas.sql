-- 023 — CRON CORRIDAS (PR2.2a monitoreo, 2026-06-29): registro UNIFICADO de cada
-- corrida de un job nocturno (backup, CLS, microglía, status, relevance, curar, DMN).
--
-- El problema: hoy los jobs nocturnos solo dejan rastro en los LOGS del worker
-- (efímeros) → el monitoreo (/salud nocturno) no puede decir "¿corrió el backup
-- anoche? ¿cuándo? ¿con qué resultado?". dmn_corridas solo cubre las tasks del DMN,
-- no los jobs principales. Esta tabla los registra TODOS, con timestamp, de forma
-- medible y persistente (sobrevive reinicios) — base de las alertas (PR2.2b).
--
-- DB-backed (estilo For3s): cada corrida auditada y medible.

CREATE TABLE IF NOT EXISTS cron_corridas (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job         TEXT NOT NULL,                          -- backup | cls | microglia | status | relevance | curar_skills | dmn_noche | dmn_idle | health_check
    ok          BOOLEAN NOT NULL DEFAULT true,          -- ¿terminó sin error?
    resultado   TEXT NOT NULL DEFAULT '',               -- el mensaje del job (lo que hoy va al log)
    ms          INT NOT NULL DEFAULT 0,                  -- duración en milisegundos
    creado_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- ⭐ CUÁNDO corrió (el timestamp que faltaba)
);

-- consultar "última corrida de cada job" rápido (lo que usa /salud nocturno)
CREATE INDEX IF NOT EXISTS idx_cron_corridas_job_fecha
    ON cron_corridas (job, creado_at DESC);
