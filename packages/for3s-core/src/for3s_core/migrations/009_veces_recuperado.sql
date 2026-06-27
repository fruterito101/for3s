-- 009 — Contador de recuperaciones para el refuerzo por uso (H6 relevance v2, 2026-06-22).
--
-- Añade veces_recuperado a episodes_events: cuántas veces un episodio se ha
-- recuperado como recuerdo (buscar_semantico lo trajo). La fórmula de relevance
-- (relevance.py) lo usa como REFUERZO: lo más usado resiste mejor el olvido.
-- Antes la v1 tenía el refuerzo pero SIEMPRE en 0 (no se contaba) → neutro.
--
-- ADITIVA y SEGURA: NOT NULL DEFAULT 0 → las filas existentes arrancan en 0
-- (como si nunca se hubieran recuperado, que es lo correcto). No rompe nada.
ALTER TABLE episodes_events
    ADD COLUMN IF NOT EXISTS veces_recuperado INTEGER NOT NULL DEFAULT 0;