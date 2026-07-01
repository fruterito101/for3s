-- For3s OS — HA-5 (2026-06-30): matcher SEMÁNTICO de skills.
-- El matcher por palabras (buscar_relevantes) era frágil en ambos sentidos:
-- falsos positivos ("logs del servidor" disparaba la skill de deploy) y falsos
-- negativos ("despliego el bot" NO matcheaba "deploy bot servidor"). La raíz:
-- comparar palabras exactas no entiende significado ni cruza idiomas (deploy/
-- despliegue). Solución: embedding BGE-M3 (1024d) por skill + búsqueda coseno,
-- igual que episodes_events (H5). Aditivo y defensivo: si una skill no tiene
-- embedding, el matcher cae al de palabras (no rompe).

ALTER TABLE skills ADD COLUMN IF NOT EXISTS embedding vector(1024);

CREATE INDEX IF NOT EXISTS idx_skills_embedding
    ON skills USING hnsw (embedding vector_cosine_ops);
