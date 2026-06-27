-- 008 — Columnas de gobierno de memoria para H6 "SE CUIDA" (2026-06-20).
--
-- Añade a episodes_events 4 columnas que CLS (consolidación) y Microglía (olvido)
-- necesitan. ADITIVA y SEGURA, igual que la 007:
--   • Todas NULLABLE o con DEFAULT seguro → las filas existentes no se rompen.
--   • NO modifica columnas existentes, NO borra datos, NO toca el motor.
--   • Idempotente (IF NOT EXISTS).
--
-- ⚠️ IMPORTANTE (soft-delete): a partir de esta migración, deleted_at habilita el
-- borrado lógico (soft-delete) de la Microglía. TODAS las lecturas de memoria que
-- alimentan al bot DEBEN filtrar "deleted_at IS NULL" (ver memory.py: load_history
-- y buscar_semantico). Las queries de MAX(seq) NO filtran a propósito (el seq debe
-- seguir siendo monotónico aunque haya episodios soft-deleted).

-- 1) consolidated_to_kg: marca si CLS ya extrajo la lección de este episodio al
--    Knowledge Graph. Es la CONDICIÓN que Microglía exige antes de poder olvidar.
ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS consolidated_to_kg BOOLEAN NOT NULL DEFAULT false;

-- 2) relevance: relevancia del episodio (decay por desuso). Microglía solo olvida
--    lo POCO relevante. NULL = aún sin calcular (se llena en el Sub-paso 3).
ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS relevance REAL;

-- 3) last_accessed: última vez que el episodio fue recuperado (base del decay).
--    NULL = nunca recuperado desde que existe esta columna.
ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS last_accessed TIMESTAMPTZ;

-- 4) deleted_at: SOFT-DELETE. NULL = vivo; con fecha = olvidado por Microglía
--    (recuperable poniendo deleted_at = NULL). Microglía NUNCA hace DELETE físico.
ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- 5) índice parcial: cola de "pendientes por consolidar" que CLS lee cada noche.
--    Parcial (solo false) → pequeño y rápido; se vacía a medida que CLS consolida.
CREATE INDEX IF NOT EXISTS idx_episodes_pending_consolidation
    ON episodes_events (session_id, seq)
    WHERE consolidated_to_kg = false;

-- 6) índice parcial: filtro de "vivos" que usan TODAS las lecturas del bot.
CREATE INDEX IF NOT EXISTS idx_episodes_vivos
    ON episodes_events (session_id, seq)
    WHERE deleted_at IS NULL;