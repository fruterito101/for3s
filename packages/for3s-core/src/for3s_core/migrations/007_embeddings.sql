-- 007 — Columna de embeddings para búsqueda semántica de memoria (H5, 2026-06-19).
--
-- Añade a episodes_events una columna 'embedding' que guardará el vector de 1024
-- dimensiones (BGE-M3) de cada turno. ADITIVA y SEGURA:
--   • NULLABLE → los turnos existentes quedan con embedding=NULL (no se rompe nada;
--     el bot sigue leyendo/escribiendo igual). Se llenan en el backfill (sub-paso 5).
--   • NO modifica ninguna columna existente, NO borra datos, NO toca el motor.
--   • Idempotente (IF NOT EXISTS).
--
-- El índice HNSW acelera la búsqueda por similitud coseno. vector_cosine_ops =
-- distancia coseno (la que usa la búsqueda semántica). Con la columna vacía el
-- índice se crea instantáneo y se va poblando a medida que se llenan embeddings.

-- 1) columna de embedding (1024 dim, la salida de BGE-M3) — NULLABLE
ALTER TABLE episodes_events ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- 2) índice HNSW para búsqueda por similitud coseno
CREATE INDEX IF NOT EXISTS idx_episodes_embedding
    ON episodes_events USING hnsw (embedding vector_cosine_ops);
