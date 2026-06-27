-- 017 — Expulsión de miembros (C-v sacar/denegar, 2026-06-24).
--
-- El encargado puede SACAR a un miembro del equipo. Soft-remove: activo=false
-- (pierde acceso, su historial NO se borra). Además `expulsado`=true marca que
-- fue sacado EXPLÍCITAMENTE → NO re-entra por la puerta abierta (sacar = denegar
-- de verdad); solo vuelve si el encargado lo re-invita. Distingue "nunca entró"
-- de "fue sacado".
--
-- ADITIVA: columna nueva con default false (los miembros actuales no están
-- expulsados). Reversible: re-invitar pone activo=true, expulsado=false.

ALTER TABLE equipo_miembros
    ADD COLUMN IF NOT EXISTS expulsado BOOLEAN NOT NULL DEFAULT false;
