"""For3s OS — version-self-awareness (AI5, 2026-06-23). Cierra P4 + G4.

Fuente ÚNICA de verdad de la versión del agente. For3s se construye por HITOS
(H1-H8); la versión refleja eso: semver + hito. Aquí vive el CHANGELOG que el
PROPIO agente puede consultar para responder "¿qué versión eres? ¿qué hay nuevo?"
(antes no podía — G4, lo detectó el propio agente).

Cómo se usa: el bot detecta una pregunta de versión/cambios e inyecta resumen()
al contexto (barato, solo cuando preguntan). También hay comando /version.

⚠️ MANTENER: al cerrar un hito o pulido importante, actualizar VERSION + añadir una
entrada al inicio de CHANGELOG. Un solo lugar.
"""

from __future__ import annotations

# ── Versión actual ─────────────────────────────────────────────────────────
VERSION = "0.12.0"  # semver (0.x = alpha; H10 PLANEA — metacognición)
HITO = "H10 PLANEA"  # hito actual
HITO_DESC = "metacognición: sé cuándo NO sé (mido mi confianza antes de afirmar)"
FASE = "H10-PLANEA v1 (confidence scoring + honestidad en baja confianza, en chat)"

# ── Changelog por HITOS (lo nuevo arriba) ──────────────────────────────────
# Cada entrada: (versión, hito, fecha, [qué trae]). El agente lee esto para
# responder qué cualidades nuevas y viejas tiene.
CHANGELOG: list[dict] = [
    {
        "version": "0.12.0",
        "hito": "H10 PLANEA",
        "fecha": "2026-06-26",
        "cambios": [
            "Metacognición: mido mi propia confianza antes de afirmar algo",
            "Sé cuándo NO sé: si dudo, lo digo o pido aclaración (no invento)",
            "Confidence scoring con señales reales (mi fraseo + histórico) + audit",
            "Si mi confianza es baja, marco la respuesta como tentativa",
        ],
    },
    {
        "version": "0.11.0",
        "hito": "H9 SUEÑA",
        "fecha": "2026-06-26",
        "cambios": [
            "DMN: trabajo solo cuando estás inactivo (mantenimiento + auto-mejora)",
            "Housekeeping: pre-computo embeddings, consolido memoria, vigilo mi calidad",
            "Generativas (gobernadas): detecto patrones e hipótesis → te las propongo",
            "Comando /dmn (status/on/off/correr/propuestas/roi) — solo el dueño",
            "ROI por task: mido qué aporta cada tarea de fondo y qué conviene apagar",
        ],
    },
    {
        "version": "0.10.0",
        "hito": "H12 APRENDE",
        "fecha": "2026-06-25",
        "cambios": [
            "/aprende: For3s destila una skill (receta) de lo que acaban de trabajar",
            "Auto-mejora: tras tareas complejas propone skills (espera tu aprobación)",
            "Toda skill nueva pasa por el governor (scanner de seguridad) antes de guardarse",
            "Curación nocturna: las skills auto sin uso se archivan solas (recuperable)",
            "Cierra el ciclo APRENDE (H10 tener+usar · H11 freno · H12 crear+mejorar)",
        ],
    },
    {
        "version": "0.9.0",
        "hito": "H11 GOVERNOR",
        "fecha": "2026-06-25",
        "cambios": [
            "Skills (recetas reutilizables): For3s puede tener y aplicar SKILL.md (/skills)",
            "Governor de skills: escanea toda skill nueva en busca de patrones peligrosos",
            "Kill switch de auto-generación (/autogen on|off|status), apagado por defecto",
            "Frenos: techo diario de auto-creación, no duplicar, techo de skills activas",
            "Las skills creadas por una persona son intocables por el sistema (provenance)",
        ],
    },
    {
        "version": "0.8.3",
        "hito": "H8 EQUIPO — pulido",
        "fecha": "2026-06-23",
        "cambios": [
            "Temas por persona en Telegram (/tema, /temas): un chat = varios hilos separados",
            "Audit trail del equipo multi-agente en BD (cada corrida queda registrada)",
            "Hilo por usuario: cada persona su conversación, sin mezclarse (bug crítico resuelto)",
            "El equipo multi-agente muestra progreso en vivo + gasto de tokens",
            "Menú de comandos en Telegram por rol",
        ],
    },
    {
        "version": "0.8.0",
        "hito": "H8 EQUIPO",
        "fecha": "2026-06-23",
        "cambios": [
            "Trabajo en EQUIPO multi-agente: 5 specialists en paralelo + síntesis (2 familias)",
            "MULTI-USUARIO: varias personas un mismo agente, roles, puerta /invitar",
            "Memoria híbrida: privada por persona + común del equipo",
            "Gate de aprobación del encargado para acciones sensibles",
        ],
    },
    {
        "version": "0.7.0",
        "hito": "H7 (parcial) /model",
        "fecha": "2026-06-23",
        "cambios": [
            "Comando /model: elegir el modelo de IA (Haiku/Sonnet/Opus)",
            "Enrutamiento automático por costo: BLOQUEADO por decisión (suscripción plana)",
        ],
    },
    {
        "version": "0.6.0",
        "hito": "H6 SE CUIDA",
        "fecha": "2026-06-20",
        "cambios": [
            "Se mantiene solo de noche: backup + consolidación (CLS) + olvido (Microglía)",
            "La memoria se organiza y mejora sola mientras nadie la usa",
        ],
    },
    {
        "version": "0.5.0",
        "hito": "H5 MEMORIA REAL",
        "fecha": "2026-06-20",
        "cambios": [
            "Memoria semántica: recuerda por SIGNIFICADO en todo el historial",
            "Knowledge Graph (conceptos, repos, issues) que se puebla al leer GitHub",
        ],
    },
    {
        "version": "0.4.0",
        "hito": "MVP (H1-H4)",
        "fecha": "2026-06-19",
        "cambios": [
            "Chat con memoria persistente (Telegram + CLI)",
            "Análisis de repos GitHub + write tools seguras con confirmación",
            "Multimodal (imágenes/PDF/Word/Excel) + web fetch + cifrado KEK + audit",
        ],
    },
]


def resumen(schema_version: int | None = None) -> str:
    """Texto listo para inyectar al agente o mostrar en /version. Incluye versión
    actual + hito + lo más nuevo. schema_version (de la BD) es un dato técnico
    opcional. NO inventa: solo lo que está aquí declarado."""
    lineas = [
        "VERSIÓN DE FOR3S OS (datos reales, NO los inventes):",
        f"• Versión: {VERSION} — hito {HITO} ({HITO_DESC}).",
        f"• Fase actual: {FASE}.",
    ]
    if schema_version is not None:
        lineas.append(f"• Esquema de base de datos: v{schema_version} (dato técnico interno).")
    nuevo = CHANGELOG[0]
    lineas.append(f"• Lo MÁS NUEVO ({nuevo['fecha']}, {nuevo['hito']}):")
    for c in nuevo["cambios"]:
        lineas.append(f"   - {c}")
    lineas.append(
        "• Hitos completos hasta hoy: H1·H2·H3·H4 (MVP) → H5 (memoria) → "
        "H6 (se cuida) → H7 /model → H8 (equipo+multiusuario)."
    )
    return "\n".join(lineas)


def resumen_corto() -> str:
    """Una línea para /estado u otros lugares."""
    return f"For3s OS v{VERSION} — {HITO} ({HITO_DESC})"
