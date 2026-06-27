-- 012 — Gate de aprobación del encargado (H8 S10d, 2026-06-23).
--
-- Un MIEMBRO no ejecuta acciones sensibles (escribir en GitHub, borrar) por su
-- cuenta: las PROPONE y el ENCARGADO aprueba/rechaza (matriz de permisos S10b,
-- nivel "propone"). Esta tabla guarda esas solicitudes pendientes.
--
-- estado: 'pendiente' → 'aprobada' | 'rechazada'. El payload (qué acción, con
-- qué datos) va en JSONB para no atarnos a un tipo de acción concreto.

CREATE TABLE IF NOT EXISTS solicitudes (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    equipo_id     BIGINT NOT NULL REFERENCES equipos(id),
    solicitante_id BIGINT NOT NULL,          -- user_id del miembro que la pide
    accion        TEXT NOT NULL,             -- p.ej. 'accion_sensible'
    descripcion   TEXT NOT NULL,             -- texto legible de qué se pide
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- datos para ejecutar
    estado        TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente|aprobada|rechazada
    resuelta_por  BIGINT,                    -- user_id del encargado que decidió
    creada_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    resuelta_at   TIMESTAMPTZ
);

-- buscar rápido las pendientes de un equipo
CREATE INDEX IF NOT EXISTS idx_solicitudes_pendientes
    ON solicitudes (equipo_id) WHERE estado = 'pendiente';
