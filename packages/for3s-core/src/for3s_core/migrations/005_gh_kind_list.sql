-- 005 — ampliar gh_resources.kind para incluir listados y búsquedas.
-- Antes el CHECK solo permitía pr/issue/file/gist → los listados (list_issues,
-- list_pull_requests) y search_code no se podían persistir. Ahora también
-- 'list' (un listado de issues/PRs) y 'search'. Aditiva, idempotente.

ALTER TABLE gh_resources DROP CONSTRAINT IF EXISTS gh_resources_kind_check;
ALTER TABLE gh_resources
    ADD CONSTRAINT gh_resources_kind_check
    CHECK (kind IN ('pr', 'issue', 'file', 'gist', 'list', 'search'));
