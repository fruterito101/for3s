-- For3s OS — F1 del REDISEÑO MEMORIA (2026-07-01): tabla PERSONAS canónica.
-- Ronda: Cuerpo/Ronda_Rediseno_Memoria_Plan.md §F1.
--
-- PROBLEMA (medido): la identidad de una persona vivía repartida en 5 nombres
-- (telegram_user_id, user_id, owner_user_id, session_id embebido, sessions.id) y NO
-- existía UNA tabla que listara a todas las personas → las FKs de MEM-1 no tenían a
-- dónde apuntar. Esta tabla es el ANCLA canónica.
--
-- CANÓNICO: PERSONA = telegram_user_id (bigint). SESIÓN = 'tg:'||telegram_user_id[:tema].
-- Aditivo y de bajo riesgo: SOLO crea+puebla una tabla nueva; NO toca las existentes
-- (la migración de datos y las FKs son F3, aparte, con su propio backup).

CREATE TABLE IF NOT EXISTS personas (
    telegram_user_id  bigint PRIMARY KEY,          -- identidad canónica de PERSONA
    nombre            text,                          -- nombre para mostrar (si se conoce)
    rol               text,                          -- encargado | miembro | (owner)
    creada_at         timestamptz NOT NULL DEFAULT now(),
    actualizada_at    timestamptz NOT NULL DEFAULT now()
);

-- Poblar desde equipo_miembros (los usuarios conocidos con su rol)
INSERT INTO personas (telegram_user_id, nombre, rol)
    SELECT user_id, NULLIF(nombre,''), rol
    FROM equipo_miembros
    WHERE activo
ON CONFLICT (telegram_user_id) DO UPDATE
    SET rol = EXCLUDED.rol,
        nombre = COALESCE(personas.nombre, EXCLUDED.nombre),
        actualizada_at = now();

-- Poblar también cualquier autor de episodes que no esté aún (cobertura completa)
INSERT INTO personas (telegram_user_id)
    SELECT DISTINCT telegram_user_id
    FROM episodes_events
    WHERE telegram_user_id IS NOT NULL
ON CONFLICT (telegram_user_id) DO NOTHING;

-- El dueño (owner) siempre presente, marcado como encargado si no tiene rol
INSERT INTO personas (telegram_user_id, rol)
    SELECT owner_id, 'encargado' FROM owner WHERE workspace='default'
ON CONFLICT (telegram_user_id) DO UPDATE
    SET rol = COALESCE(personas.rol, 'encargado'), actualizada_at = now();
