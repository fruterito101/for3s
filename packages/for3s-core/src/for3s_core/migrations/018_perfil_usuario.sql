-- 018 — Perfil de usuario (P1 modelar al usuario, 2026-06-24).
--
-- For3s recuerda QUÉ se habló (memoria semántica), pero no QUIÉN es cada persona.
-- Esta tabla guarda un PERFIL por persona: campos clave (rol, stack, zona, estilo)
-- + rasgos libres que el bot aprende. Se inyecta al contexto al responderle para
-- adaptar el enfoque a quién es. Híbrido: la persona lo dice / el bot infiere y
-- confirma. Por PERSONA, global (aplica en todos sus temas/hilos).
--
-- ADITIVA: tabla nueva, telegram_user_id como PK (un perfil por persona).

CREATE TABLE IF NOT EXISTS perfil_usuario (
    telegram_user_id BIGINT PRIMARY KEY,
    nombre           TEXT,                    -- nombre legible
    rol              TEXT,                    -- ej. "backend", "frontend", "PM"
    stack            TEXT,                    -- herramientas/lenguajes preferidos
    estilo           TEXT,                    -- estilo de respuesta preferido
    zona             TEXT,                    -- zona horaria / ubicación
    rasgos           JSONB NOT NULL DEFAULT '[]'::jsonb,  -- rasgos libres aprendidos
    actualizado_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    creado_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
