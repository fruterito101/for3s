-- 022 — Propuestas del DMN generativo (H9-c, 2026-06-25).
--
-- Las tasks GENERATIVAS del DMN (se ejecutan en el WORKER, sin Telegram directo)
-- dejan aquí sus salidas para que el DUEÑO las revise y apruebe/descarte desde el
-- bot (/dmn propuestas). Patrón DB-backed de For3s: el worker no depende de la red
-- de Telegram, y todo queda auditado. Plan: Cuerpo/H9_SUENA_Plan_Maestro_DMN.md.
--
-- NADA aquí se auto-aplica: una propuesta vive hasta que el dueño la apruebe o
-- descarte (igual filosofía que el gate de skills de H12). prompt_improvement (que
-- tocaría la personalidad) es stub en v1 → cruza con el pendiente AUTO-CONCIENCIA AC3.

CREATE TABLE IF NOT EXISTS dmn_propuestas (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace   TEXT NOT NULL DEFAULT 'default',
    task        TEXT NOT NULL,            -- hypothesis_generation | pattern_detection | ...
    tipo        TEXT NOT NULL,            -- hipotesis | patron | mejora_prompt
    titulo      TEXT NOT NULL,            -- resumen corto (para el listado)
    contenido   TEXT NOT NULL,            -- la propuesta completa
    estado      TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente | aprobada | descartada
    costo_usd   NUMERIC(10,4) NOT NULL DEFAULT 0,
    resuelta_por BIGINT,                  -- telegram_user_id del dueño que decidió
    resuelta_at TIMESTAMPTZ,
    creada_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dmn_propuestas_pendientes
    ON dmn_propuestas (estado, creada_at DESC) WHERE estado = 'pendiente';
