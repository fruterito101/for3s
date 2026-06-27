"""For3s OS — catálogo de SPECIALISTS para el equipo multi-agente (H8, 2026-06-23).

H8 convierte a For3s de 1 agente a un EQUIPO hub-and-spoke: el Hub lanza N specialists
en paralelo sobre una tarea y un Synthesizer combina sus reportes. Este módulo define:
  · S1: el CATÁLOGO (las "fichas" de cada specialist).
  · S2: `correr_specialist()` — ejecuta UN specialist (su rol + LLM + límites), aislado.
El spawn en PARALELO, message bus, hub y synthesizer vienen en S3+. Aquí todo es de a UNO.

DOS FAMILIAS (decisión de diseño — For3s es segundo cerebro UNIVERSAL, no solo código):
  · TECNICA: análisis de código/repos/PRs (los 5 del diseño LOCKED R5 B3).
  · GENERAL: tareas no-código (escribir, investigar, decidir, planear, organizar).
El Hub (S4) elige la familia según la tarea (URL GitHub → técnica; lo demás → general).

⚠️ Cada specialist tiene una WHITELIST de tools (las únicas que puede usar — capa de
aislamiento, S9) + límites (timeout, token budget — para el cost control, S8).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("for3s.specialists")

# S9 — AISLAMIENTO: contexto aislado por specialist (ContextVar, capa 1). Cada
# specialist corre como asyncio.task con su propio valor → no se pisan entre sí.
# Base que el multi-usuario (S10) reusa para aislar lo que ve cada persona.
_ctx_specialist: contextvars.ContextVar[str] = contextvars.ContextVar(
    "specialist_actual", default=""
)

# Write tools de GitHub — los specialists son READ-ONLY (mutation guard, capa 6):
# NINGÚN specialist puede ejecutar estas. Escribir es del usuario/encargado, NO de
# un agente autónomo. (Lista espejo de WRITE_TOOLS_PERMITIDAS de tool_loop.py.)
_WRITE_TOOLS = frozenset(
    {
        "add_issue_comment",
        "create_issue",
        "create_pull_request",
        "create_pull_request_review",
    }
)


def tool_permitida(definicion: SpecialistDefinition, tool: str) -> tuple[bool, str]:
    """¿Puede este specialist usar esta tool? (capas 2 whitelist + 6 mutation guard).
    Rechaza: (a) cualquier write tool (read-only enforcement), (b) toda tool fuera de
    su whitelist. Devuelve (permitida, razón)."""
    if tool in _WRITE_TOOLS:
        return False, "los specialists son read-only (no pueden escribir/mutar)"
    if tool not in definicion.tools:
        return False, f"'{tool}' no está en la whitelist de {definicion.nombre}"
    return True, "ok"


@dataclass(frozen=True)
class SpecialistDefinition:
    """La ficha de un specialist: quién es, qué puede usar, sus límites."""

    nombre: str
    familia: str  # "tecnica" | "general"
    rol: str  # system prompt: quién es y qué hace
    tools: tuple[str, ...] = ()  # whitelist de tools permitidas (vacío = sin tools)
    timeout_seg: int = 120  # tope de tiempo por specialist
    max_tokens: int = 1500  # presupuesto de tokens (cost control)


# tools de GitHub de solo-lectura (las que ya existen en MVP_TOOLS, sin write)
_GH_READ = (
    "list_issues",
    "list_pull_requests",
    "get_file_contents",
    "search_code",
    "search_issues",
    "search_pull_requests",
)


# ============================================================================
# FAMILIA TÉCNICA / QA (diseño LOCKED R5 B3) — análisis de código
# ============================================================================
_TECNICA: list[SpecialistDefinition] = [
    SpecialistDefinition(
        nombre="code_analyzer",
        familia="tecnica",
        rol=(
            "Eres Code Analyzer, experto en analizar QUÉ HACE el código: su "
            "estructura, lógica, arquitectura y flujo. Explica con claridad qué "
            "es y cómo funciona. NO inventes: solo afirma lo que viste en el código."
        ),
        tools=_GH_READ,
    ),
    SpecialistDefinition(
        nombre="security_auditor",
        familia="tecnica",
        rol=(
            "Eres Security Auditor. Tu único foco son las VULNERABILIDADES y riesgos "
            "de seguridad: inyecciones, secretos expuestos, validación faltante, "
            "permisos, deps inseguras. Reporta hallazgos concretos, sin alarmismo."
        ),
        tools=_GH_READ,
    ),
    SpecialistDefinition(
        nombre="test_generator",
        familia="tecnica",
        rol=(
            "Eres Test Generator. Evalúas la COBERTURA de tests: qué está probado, "
            "qué falta, casos borde sin cubrir. Sugiere qué tests añadir y por qué."
        ),
        tools=_GH_READ,
    ),
    SpecialistDefinition(
        nombre="performance_analyzer",
        familia="tecnica",
        rol=(
            "Eres Performance Analyzer. Identificas CUELLOS DE BOTELLA y problemas "
            "de rendimiento: bucles costosos, queries N+1, I/O bloqueante, memoria. "
            "Señala dónde y por qué, con su impacto."
        ),
        tools=_GH_READ,
    ),
    SpecialistDefinition(
        nombre="doc_writer",
        familia="tecnica",
        rol=(
            "Eres Doc Writer. Evalúas y mejoras la DOCUMENTACIÓN: claridad del README, "
            "comentarios, ejemplos de uso. Señala qué falta documentar y propón mejoras."
        ),
        tools=_GH_READ,
    ),
]


# ============================================================================
# FAMILIA GENERAL / CONOCIMIENTO (NUEVA, 2026-06-23) — tareas no-código
# ============================================================================
_GENERAL: list[SpecialistDefinition] = [
    SpecialistDefinition(
        nombre="investigador",
        familia="general",
        rol=(
            "Eres Investigador. Buscas y SINTETIZAS información relevante: contexto, "
            "fuentes, datos. Distingues lo que sabes de lo que necesitarías verificar. "
            "Honesto: si no tienes el dato, lo dices, no lo inventas."
        ),
        tools=("fetch_url",),  # puede leer páginas web públicas
    ),
    SpecialistDefinition(
        nombre="escritor",
        familia="general",
        rol=(
            "Eres Escritor. Redactas y MEJORAS textos (correos, propuestas, documentos, "
            "contenido): claros, bien estructurados, con el tono adecuado. Propón el "
            "texto listo para usar."
        ),
    ),
    SpecialistDefinition(
        nombre="analista",
        familia="general",
        rol=(
            "Eres Analista. DESCOMPONES un problema o decisión: pros/contras, opciones, "
            "implicaciones, qué datos faltan. Das una recomendación razonada, no vaga."
        ),
    ),
    SpecialistDefinition(
        nombre="planificador",
        familia="general",
        rol=(
            "Eres Planificador. ESTRUCTURAS planes y organizas ideas: pasos concretos, "
            "orden, dependencias, hitos. Conviertes algo difuso en un plan accionable."
        ),
    ),
    SpecialistDefinition(
        nombre="critico",
        familia="general",
        rol=(
            "Eres Crítico/Revisor (el abogado del diablo, constructivo). CUESTIONAS: "
            "detectas huecos, supuestos no validados, riesgos, qué podría salir mal. "
            "Tu objetivo es FORTALECER la idea, no destruirla."
        ),
    ),
]


# catálogo completo: nombre → definición
CATALOGO: dict[str, SpecialistDefinition] = {s.nombre: s for s in (_TECNICA + _GENERAL)}

FAMILIAS = ("tecnica", "general")


def de_familia(familia: str) -> list[SpecialistDefinition]:
    """Devuelve los specialists de una familia ('tecnica' | 'general')."""
    return [s for s in CATALOGO.values() if s.familia == familia]


def get(nombre: str) -> SpecialistDefinition | None:
    """Devuelve la definición de un specialist por nombre, o None."""
    return CATALOGO.get(nombre)


# ============================================================================
# S2 — RUNNER de UN specialist (sin paralelo aún; eso es S4)
# ============================================================================


@dataclass(frozen=True)
class ResultadoSpecialist:
    """Lo que devuelve un specialist tras analizar su parte."""

    nombre: str
    ok: bool
    texto: str  # su análisis (o el mensaje de error si ok=False)
    tokens_in: int = 0
    tokens_out: int = 0
    segundos: float = 0.0


async def correr_specialist(
    definicion: SpecialistDefinition,
    entrada: str,
    *,
    provider=None,
) -> ResultadoSpecialist:
    """Ejecuta UN specialist sobre `entrada` y devuelve su análisis (S2).

    OAUTH-SAFE: el rol va en el USER message, system="" (regla del 429-system).
    Respeta su timeout_seg y max_tokens. DEFENSIVA: si falla (LLM, timeout, 429),
    devuelve ok=False con el motivo — NUNCA lanza (un specialist caído no debe
    tumbar al equipo; eso lo aprovecha el Hub en S4). Mide tokens y tiempo para
    saber el costo real (clave para decidir el espaciado del paralelo en S4).

    provider: un ClaudeProvider ya construido (se reusa entre specialists). Si es
    None, lo crea desde settings.
    """
    t0 = time.time()
    # S9 capa 1: aislar el contexto de ESTE specialist (ContextVar per-task). El
    # specialist NUNCA recibe el Master KEK (capa 3 KEK scoping): correr_specialist
    # solo recibe la definición + entrada + provider; no hay acceso al SecretStore
    # ni a la master.key desde aquí → un specialist no puede leer secretos.
    _ctx_specialist.set(definicion.nombre)
    try:
        if provider is None:
            from for3s_core.config import load_settings
            from for3s_core.llm import ClaudeProvider

            s = load_settings()
            provider = ClaudeProvider(token=s.anthropic_token, oauth=s.is_oauth, model=s.model)

        # rol + entrada juntos en el USER message; system vacío (OAuth-safe)
        prompt = f"[{definicion.rol}]\n\n{entrada}"
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                provider.complete,
                prompt,
                system="",
                max_tokens=definicion.max_tokens,
            ),
            timeout=definicion.timeout_seg,
        )
        dt = time.time() - t0
        logger.info(
            "[specialist:%s] ok (%.1fs, in=%d out=%d)",
            definicion.nombre,
            dt,
            resp.input_tokens,
            resp.output_tokens,
        )
        return ResultadoSpecialist(
            nombre=definicion.nombre,
            ok=True,
            texto=resp.text,
            tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens,
            segundos=dt,
        )
    except TimeoutError:
        dt = time.time() - t0
        logger.warning("[specialist:%s] TIMEOUT (%.0fs)", definicion.nombre, dt)
        return ResultadoSpecialist(
            nombre=definicion.nombre,
            ok=False,
            texto=f"(timeout: {definicion.nombre} tardó más de {definicion.timeout_seg}s)",
            segundos=dt,
        )
    except Exception as e:  # noqa: BLE001 — un specialist caído NO tumba el equipo
        dt = time.time() - t0
        logger.warning("[specialist:%s] falló: %s", definicion.nombre, type(e).__name__)
        return ResultadoSpecialist(
            nombre=definicion.nombre,
            ok=False,
            texto=f"({definicion.nombre} no pudo completar: {type(e).__name__})",
            segundos=dt,
        )
