-- 021 — DMN "SUEÑA" (H9, 2026-06-25): el sistema trabaja solo cuando está idle.
--
-- El DMN (Default Mode Network, Nodo 6) corre tasks en background cuando nadie usa
-- el sistema: housekeeping (se mantiene) + generativas (se mejora, gobernadas por H11).
-- Esta migración crea el REGISTRO de corridas (base del ROI H9-d + la verificación) y
-- el estado/kill switch del DMN. Plan: Cuerpo/H9_SUENA_Plan_Maestro_DMN.md.
--
-- DB-backed (estilo For3s): cada corrida queda auditada y medible, sobrevive reinicios.

-- Registro de cada corrida de una task del DMN (append-only). Base del ROI.
CREATE TABLE IF NOT EXISTS dmn_corridas (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace     TEXT NOT NULL DEFAULT 'default',
    task          TEXT NOT NULL,            -- nombre de la task (embedding_precompute, ...)
    clase         TEXT NOT NULL,            -- housekeeping | generativa
    trigger_ok    BOOLEAN NOT NULL,         -- ¿el trigger dijo que valía correr?
    corrio        BOOLEAN NOT NULL DEFAULT false,  -- ¿se ejecutó la action?
    outcome       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- métrica de resultado (qué produjo)
    costo_usd     NUMERIC(10,4) NOT NULL DEFAULT 0,    -- costo estimado de la corrida
    ms            INT NOT NULL DEFAULT 0,    -- duración en milisegundos
    motivo        TEXT NOT NULL DEFAULT '',  -- nota legible (por qué no corrió, error, etc.)
    creado_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dmn_corridas_task_fecha
    ON dmn_corridas (task, creado_at DESC);
CREATE INDEX IF NOT EXISTS idx_dmn_corridas_fecha
    ON dmn_corridas (creado_at DESC);

-- Estado-singleton del DMN por workspace (kill switch por clase de task).
-- Default: housekeeping ON (seguro, auto-aplica), generativas OFF (conservador,
-- se encienden tras calibrar — igual que el autogen del governor).
CREATE TABLE IF NOT EXISTS dmn_estado (
    workspace        TEXT PRIMARY KEY DEFAULT 'default',
    housekeeping_on  BOOLEAN NOT NULL DEFAULT true,
    generativas_on   BOOLEAN NOT NULL DEFAULT false,
    cambiado_por     BIGINT,                 -- telegram_user_id del dueño
    cambiado_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    motivo           TEXT NOT NULL DEFAULT ''
);

INSERT INTO dmn_estado (workspace, motivo)
VALUES ('default', 'estado inicial — housekeeping ON, generativas OFF hasta calibrar')
ON CONFLICT (workspace) DO NOTHING;
