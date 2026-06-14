-- 004 — Persistencia de recursos GitHub (Hueco 1 del anexo R4.2.1).
-- Hasta ahora los datos de GitHub (PR/issue traído) se usaban una vez y se
-- tiraban: solo quedaba el reporte de Claude en episodes_events. Esto guarda
-- un SNAPSHOT estructurado de cada recurso traído, consultable por los H
-- siguientes (memoria H5, knowledge graph H6, etc.).
--
-- Cache (Valkey, R4) ≠ Persistencia (esto). Append-friendly: un nuevo fetch
-- del mismo PR = nuevo snapshot con su fetched_at (no UPDATE destructivo).
-- Aditiva e idempotente.

-- gh_resources: un snapshot de cada recurso de GitHub traído (PR/issue/file/gist)
CREATE TABLE IF NOT EXISTS gh_resources (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    session_id    TEXT REFERENCES sessions(id),   -- en qué conversación se trajo
    kind          TEXT NOT NULL CHECK (kind IN ('pr','issue','file','gist')),
    owner         TEXT NOT NULL,
    repo          TEXT NOT NULL,
    number        INTEGER,            -- PR/issue number (NULL para file/gist)
    path          TEXT,               -- para file/gist
    title         TEXT,
    author        TEXT,
    state         TEXT,               -- open/closed/merged
    body          TEXT,               -- descripción/cuerpo
    raw           JSONB NOT NULL DEFAULT '{}'::jsonb,  -- metadata completa estructurada
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gh_res_lookup ON gh_resources (workspace_id, owner, repo, kind, number);
CREATE INDEX IF NOT EXISTS idx_gh_res_session ON gh_resources (session_id);

-- gh_files: archivos/diffs asociados a un recurso (1 PR → N archivos)
CREATE TABLE IF NOT EXISTS gh_files (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resource_id   BIGINT NOT NULL REFERENCES gh_resources(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    status        TEXT,               -- added/modified/removed
    additions     INTEGER DEFAULT 0,
    deletions     INTEGER DEFAULT 0,
    patch         TEXT,               -- diff (posiblemente truncado)
    content       TEXT,               -- contenido completo si se trajo (get_file_contents)
    truncated     BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_gh_files_resource ON gh_files (resource_id);
