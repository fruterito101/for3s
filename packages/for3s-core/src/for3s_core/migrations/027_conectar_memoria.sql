-- For3s OS — F3 del REDISEÑO MEMORIA (2026-07-01): CONECTAR la memoria a personas.
-- Ronda: Cuerpo/Ronda_Rediseno_Memoria_Plan.md §F3.
--
-- Conecta las tablas de persona a la identidad canónica (tabla personas, F1) con
-- Foreign Keys. Análisis previo verificó: personas cubre TODOS los uid (0 huérfanos).
--
-- ⚠️ DECISIÓN CLAVE (análisis de comportamiento): las FKs son NULLABLE. episodes tiene
-- 563 turnos legado + flujos (CLI/worker/DMN) que guardan sin telegram_user_id → una FK
-- NOT NULL rompería esos INSERT. Una FK sobre columna nullable NO valida las filas NULL
-- (semántica estándar de Postgres): valida los que SÍ tienen valor, deja pasar los NULL.
--
-- Backfill primero: los 563 legado de tg:1923367928 son de Brian (owner+session lo
-- confirman) → rellenar su telegram_user_id. Luego las FKs. Todo aditivo/seguro.

BEGIN;

-- 1) BACKFILL: rellenar telegram_user_id del legado (derivado del owner_user_id, que
--    coincide con el uid embebido en session_id 'tg:<uid>'). Solo donde falta.
UPDATE episodes_events
SET telegram_user_id = owner_user_id
WHERE deleted_at IS NULL
  AND telegram_user_id IS NULL
  AND owner_user_id IS NOT NULL;

-- 2) FKs NULLABLE a personas (validan los valores presentes, dejan pasar NULL).
--    IF NOT EXISTS-equivalente: se envuelve cada una para que la migración sea
--    re-ejecutable sin error si ya existe.
DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_perfil_persona') THEN
        ALTER TABLE perfil_usuario
            ADD CONSTRAINT fk_perfil_persona
            FOREIGN KEY (telegram_user_id) REFERENCES personas(telegram_user_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_temas_persona') THEN
        ALTER TABLE temas
            ADD CONSTRAINT fk_temas_persona
            FOREIGN KEY (user_id) REFERENCES personas(telegram_user_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_miembros_persona') THEN
        ALTER TABLE equipo_miembros
            ADD CONSTRAINT fk_miembros_persona
            FOREIGN KEY (user_id) REFERENCES personas(telegram_user_id);
    END IF;
    -- episodes: FK nullable (los NULL legado/CLI pasan; los presentes se validan)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_episodes_persona') THEN
        ALTER TABLE episodes_events
            ADD CONSTRAINT fk_episodes_persona
            FOREIGN KEY (telegram_user_id) REFERENCES personas(telegram_user_id);
    END IF;
END
$mig$;

COMMIT;
