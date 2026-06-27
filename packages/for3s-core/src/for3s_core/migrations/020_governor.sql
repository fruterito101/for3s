-- 020 — Governor del ecosistema de skills (H11 "APRENDE — el FRENO", 2026-06-25).
--
-- El governor es el control que DEBE existir ANTES del motor de auto-generación (H12).
-- Regla LOCKED (R6 §A, Grafo §8.4): sin freno, no hay auto-generación.
--
-- Diseño LOCKED 2026-06-25 (debate H11):
--   • Scanner de seguridad (muy conservador: bloquea + avisa) = el corazón del freno.
--   • 3 frenos sobre datos reales: generación/día (1), duplicados (4), activas (5).
--   • Frenos 2/3/6 (exploration, NO-GO health, independent eval) = hooks para H12.
--   • Kill switch SOLO del dueño (/autogen on|off). Default: auto-gen APAGADA.
--   • provenance/lifecycle ya viven en `skills` (migración 019) — aquí va el ESTADO.
--
-- Esta tabla es estado-singleton por workspace (hoy 1 workspace: 'default').
-- DB-backed (estilo For3s): el kill switch sobrevive reinicios, queda auditado.

CREATE TABLE IF NOT EXISTS governor_estado (
    workspace      TEXT PRIMARY KEY DEFAULT 'default',
    -- Kill switch: ¿está PERMITIDA la auto-generación? Default false = APAGADA.
    -- El dueño la enciende con /autogen on cuando H12 exista y él lo decida.
    autogen_on     BOOLEAN NOT NULL DEFAULT false,
    -- Quién y cuándo cambió el switch por última vez (auditoría ligera).
    cambiado_por   BIGINT,                 -- telegram_user_id del dueño
    cambiado_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Motivo opcional del último cambio (para el reporte de salud).
    motivo         TEXT NOT NULL DEFAULT ''
);

-- Fila singleton del workspace por defecto (idempotente).
INSERT INTO governor_estado (workspace, motivo)
VALUES ('default', 'estado inicial — auto-gen apagada hasta H12')
ON CONFLICT (workspace) DO NOTHING;

-- Registro inmutable de bloqueos del governor (para auditoría + reporte de salud).
-- Cada vez que el scanner o un freno RECHAZA una skill, queda aquí. Append-only.
CREATE TABLE IF NOT EXISTS governor_bloqueos (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace      TEXT NOT NULL DEFAULT 'default',
    freno          TEXT NOT NULL,          -- scanner|generacion|duplicado|activas|killswitch
    motivo         TEXT NOT NULL,          -- detalle legible (qué patrón / qué techo)
    skill_nombre   TEXT,                   -- skill afectada (si aplica)
    provenance     TEXT,                   -- usuario|auto (la que se intentó)
    creada_por     BIGINT,                 -- telegram_user_id que disparó el intento
    creado_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_governor_bloqueos_fecha
    ON governor_bloqueos (creado_at DESC);
