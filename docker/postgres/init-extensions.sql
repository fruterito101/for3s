-- For3s OS — activa las extensiones al crear la BD (C2.B).
-- Corre una sola vez, al primer arranque del contenedor postgres.
-- Las migraciones 001+ también las crean; esto garantiza disponibilidad temprana.
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
-- AGE necesita cargar su biblioteca y el search_path en cada sesión; las queries
-- del código ya hacen LOAD 'age' + SET search_path donde corresponde.
