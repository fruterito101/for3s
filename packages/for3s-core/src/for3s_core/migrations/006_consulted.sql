-- 006 — Apartados de ARCHIVOS y WEB consultados (2026-06-19).
--
-- Hasta ahora, los documentos (PDF/Word/Excel/imágenes) y las páginas web que
-- el usuario manda se procesaban y se TIRABAN: solo quedaba el mensaje + la
-- respuesta en episodes_events, pero no un registro consultable de "qué archivos
-- y qué páginas me ha mandado". Esto crea dos apartados LIGEROS para eso.
--
-- DECISIÓN DE DISEÑO: guardar SOLO metadatos + resumen, NUNCA el binario/HTML
-- completo (eso pesa y es innecesario). Cada fila tiene su columna de tiempo
-- (consulted_at) — saber CUÁNDO se consultó es clave para el panorama de cómo
-- se aloja la información. Aditiva e idempotente; NO toca tablas existentes.

-- consulted_files: cada archivo (PDF/Word/Excel/imagen) que el usuario mandó.
-- Solo tipo + nombre + resumen. SIN el binario (eso pesa megas → innecesario).
CREATE TABLE IF NOT EXISTS consulted_files (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    session_id    TEXT REFERENCES sessions(id),   -- en qué conversación se mandó
    tipo          TEXT NOT NULL,                  -- 'PDF' | 'documento Word' | 'hoja de cálculo Excel' | 'imagen'
    nombre        TEXT NOT NULL,                  -- nombre del archivo
    resumen       TEXT,                           -- resumen corto de su contenido
    consulted_at  TIMESTAMPTZ NOT NULL DEFAULT now()  -- CUÁNDO se consultó/guardó
);
CREATE INDEX IF NOT EXISTS idx_files_session ON consulted_files (session_id, consulted_at);
CREATE INDEX IF NOT EXISTS idx_files_ws ON consulted_files (workspace_id, consulted_at);

-- consulted_web: cada URL no-GitHub que el usuario mandó. Solo url + título +
-- descripción resumida. SIN el HTML completo (eso pesa → innecesario).
CREATE TABLE IF NOT EXISTS consulted_web (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    session_id    TEXT REFERENCES sessions(id),
    url           TEXT NOT NULL,
    titulo        TEXT,                           -- título de la página
    descripcion   TEXT,                           -- descripción resumida (no el HTML)
    consulted_at  TIMESTAMPTZ NOT NULL DEFAULT now()  -- CUÁNDO se consultó/guardó
);
CREATE INDEX IF NOT EXISTS idx_web_session ON consulted_web (session_id, consulted_at);
CREATE INDEX IF NOT EXISTS idx_web_ws ON consulted_web (workspace_id, consulted_at);
