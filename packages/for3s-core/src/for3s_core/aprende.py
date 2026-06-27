"""For3s OS — MOTOR /aprende (H12 "APRENDE", 2026-06-25).

For3s destila una skill (SKILL.md)
a partir de una fuente y la guarda — SIEMPRE pasando por el governor (H11) antes de
persistir. NO es un motor separado: es un PROMPT que instruye al PROPIO For3s (su
mismo LLM) a destilar la receta con el material que ya tiene en memoria.

Tres entradas (se construyen en orden de riesgo):
  • aprender_de_conversacion()  → P1: /aprende manual del dueño (fuente = el hilo).
  • proponer_skill_auto()       → P2: auto-mejora en background (provenance='auto',
                                   pasa por governor + gate al dueño; solo si /autogen ON).
  (la curación nocturna P3 vive en tasks.py, reusa H6.)

Reglas LOCKED:
  - El governor SIEMPRE evalúa (scanner incluso para /aprende manual: una conversación
    podría contener un secreto que no queremos destilar a una skill).
  - provenance='usuario' (/aprende) → directo (lo pidió un humano), pero pasa el scanner.
  - provenance='auto' (background) → frenos completos + gate al dueño.
  - OAuth-safe: instrucción en el user message, system="" (regla 429-system de For3s).

DEFENSIVO: destilar nunca rompe nada; si el LLM no produce algo válido, se reporta.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("for3s.aprende")

# Cuántos turnos recientes del hilo se le dan al LLM como fuente (suficiente para
# destilar una receta sin inflar el contexto — disciplina AI6).
TURNOS_FUENTE = 12

# Tope de tokens de la destilación (una skill es corta: cuándo usarla + pasos).
MAX_TOKENS_DESTILAR = 1200


@dataclass
class ResultadoAprende:
    """Qué pasó al intentar aprender una skill."""

    ok: bool
    mensaje: str  # explicación legible para el dueño
    skill_id: int | None = None
    nombre: str = ""
    categoria: str = ""
    requiere_gate: bool = False  # P2: se creó pero espera aprobación del dueño


# ───────────────────────── prompt de destilación ─────────────────────────
# El LLM responde SOLO un JSON con la skill (o {"vale": false} si no hay nada que
# valga). Pedimos JSON para parsear sin ambigüedad; el contenido del SKILL.md va
# dentro. Instrucción en el user message (OAuth-safe).
_INSTRUCCION = """\
Eres For3s OS. A partir del MATERIAL de abajo, destila UNA skill reutilizable: una
receta que te sirva la próxima vez que enfrentes algo parecido. Una skill NO es un
resumen de la charla: es conocimiento PROCEDIMENTAL (cuándo aplicarla + pasos claros).

Si del material NO sale una receta que valga la pena guardar (fue charla trivial, sin
un procedimiento reutilizable), dilo honestamente.

Responde SOLO con un objeto JSON válido, sin texto alrededor, con esta forma:
{
  "vale": true | false,
  "nombre": "nombre corto y claro de la skill",
  "categoria": "una palabra (ej. deploy, github, debug, escritura, general)",
  "descripcion": "una sola línea: cuándo usar esta skill",
  "tags": ["palabra1", "palabra2"],
  "contenido": "SKILL.md en markdown: un titulo + '## Cuando usarla' + '## Pasos' numerados."
}
Si "vale" es false, basta con {"vale": false, "motivo": "por qué no"}.

MATERIAL:
"""


def _extraer_json(texto: str) -> dict | None:
    """Saca el primer objeto JSON del texto del LLM (tolerante a fences ```json)."""
    if not texto:
        return None
    t = texto.strip()
    # quita fences de código si las hay
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M).strip()
    # intenta directo; si no, busca el primer {...} balanceado simple
    for candidato in (t, _primer_objeto(t)):
        if not candidato:
            continue
        try:
            d = json.loads(candidato)
            if isinstance(d, dict):
                return d
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _primer_objeto(t: str) -> str | None:
    """Devuelve el primer {...} con llaves balanceadas (heurística simple)."""
    inicio = t.find("{")
    if inicio < 0:
        return None
    nivel = 0
    for i in range(inicio, len(t)):
        if t[i] == "{":
            nivel += 1
        elif t[i] == "}":
            nivel -= 1
            if nivel == 0:
                return t[inicio : i + 1]
    return None


def _material_de_turnos(turnos, *, foco: str = "") -> str:
    """Arma el bloque MATERIAL a partir de turnos (role/content). foco opcional
    enfoca la destilación en un tema dado por el dueño."""
    lineas = []
    if foco:
        lineas.append(f"(El dueño pide enfocar la skill en: {foco})\n")
    for t in turnos:
        rol = "Usuario" if getattr(t, "role", "") == "user" else "For3s"
        contenido = (getattr(t, "content", "") or "").strip()
        if contenido:
            lineas.append(f"{rol}: {contenido}")
    return "\n".join(lineas)


async def _destilar(provider, material: str) -> dict | None:
    """Llama al LLM (OAuth-safe) para destilar la skill. Devuelve el dict parseado
    o None si no se pudo. Síncrono complete() en hilo aparte para no bloquear."""
    import asyncio

    prompt = _INSTRUCCION + material
    try:
        # complete() es síncrono (httpx) → a thread para no bloquear el event loop.
        resp = await asyncio.to_thread(
            provider.complete, prompt, system="", max_tokens=MAX_TOKENS_DESTILAR
        )
    except Exception:  # noqa: BLE001 — el LLM puede fallar (429/red); se reporta arriba
        logger.warning("destilación: el LLM falló", exc_info=True)
        return None
    return _extraer_json(getattr(resp, "text", "") or "")


async def _guardar_con_governor(
    pool, datos: dict, *, provenance: str, creada_por: int | None
) -> ResultadoAprende:
    """Pasa la skill destilada por el GOVERNOR (H11) y, si pasa, la guarda.

    provenance='usuario' → scanner + duplicado (directo, lo pidió un humano).
    provenance='auto'    → frenos completos (kill switch, techo diario, activas) + gate.
    """
    from for3s_core.governor import SkillEcosystemGovernor
    from for3s_core.skills import SkillStore

    nombre = (datos.get("nombre") or "").strip()
    contenido = (datos.get("contenido") or "").strip()
    categoria = (datos.get("categoria") or "general").strip().lower() or "general"
    descripcion = (datos.get("descripcion") or "").strip()
    tags = datos.get("tags") if isinstance(datos.get("tags"), list) else []

    if not nombre or not contenido:
        return ResultadoAprende(False, "El destilado no trajo nombre o contenido válidos.")

    gov = SkillEcosystemGovernor(pool)
    veredicto = await gov.evaluar_skill_nueva(
        nombre=nombre,
        contenido=contenido,
        categoria=categoria,
        descripcion=descripcion,
        provenance=provenance,
        creada_por=creada_por,
    )
    if not veredicto:
        detalle = (" (" + "; ".join(veredicto.detalle) + ")") if veredicto.detalle else ""
        return ResultadoAprende(
            False, f"El governor bloqueó la skill [{veredicto.freno}]: {veredicto.motivo}{detalle}"
        )

    ss = SkillStore(pool)
    sid = await ss.crear(
        nombre,
        contenido,
        categoria=categoria,
        descripcion=descripcion,
        tags=tags,
        provenance=provenance,
        creada_por=creada_por,
    )
    return ResultadoAprende(
        True, f"Skill '{nombre}' aprendida.", skill_id=sid, nombre=nombre, categoria=categoria
    )


# ───────────────────────── P1: /aprende manual ─────────────────────────
async def aprender_de_conversacion(
    pool, provider, session_id: str, *, creada_por: int | None = None, foco: str = ""
) -> ResultadoAprende:
    """P1 — el dueño hace /aprende: destila una skill de la conversación actual.

    provenance='usuario' (lo pidió un humano → directo, sin gate), pero SIEMPRE
    pasa por el scanner del governor (la charla podría contener un secreto)."""
    from for3s_core import memory

    turnos = await memory.load_history(pool, session_id, last_n=TURNOS_FUENTE)
    if not turnos:
        return ResultadoAprende(False, "No hay conversación reciente de la que aprender.")
    material = _material_de_turnos(turnos, foco=foco)
    datos = await _destilar(provider, material)
    if not datos:
        return ResultadoAprende(
            False,
            "No pude destilar la skill (el modelo no devolvió algo válido). "
            "Inténtalo de nuevo en un momento.",
        )
    if not datos.get("vale", False):
        motivo = datos.get("motivo", "no había una receta reutilizable clara")
        return ResultadoAprende(False, f"No vi una skill que valga aquí: {motivo}.")
    return await _guardar_con_governor(pool, datos, provenance="usuario", creada_por=creada_por)


# ───────────────────────── P2: auto-mejora en background ─────────────────────────
async def proponer_skill_auto(
    pool, provider, session_id: str, *, creada_por: int | None = None
) -> ResultadoAprende:
    """P2 — auto-mejora: tras una tarea compleja, For3s se pregunta solo si vale
    guardar una skill. provenance='auto' → frenos COMPLETOS del governor (incluido
    el kill switch). Solo actúa si /autogen está ON; si está OFF, can_generate niega
    y esto no crea nada (el freno funciona).

    Devuelve requiere_gate=True cuando la skill se creó y debe ir a aprobación del
    dueño (el caller —telegram_channel— dispara el gate H8). Hoy: el kill switch
    está OFF por defecto, así que en la práctica esto no genera hasta que el dueño
    lo encienda explícitamente."""
    from for3s_core.governor import SkillEcosystemGovernor

    gov = SkillEcosystemGovernor(pool)
    # Freno duro de entrada: si la auto-generación está apagada, ni siquiera destilamos
    # (ahorra tokens y respeta el kill switch).
    puede = await gov.can_generate()
    if not puede:
        return ResultadoAprende(False, f"Auto-generación frenada: {puede.motivo}")

    from for3s_core import memory

    turnos = await memory.load_history(pool, session_id, last_n=TURNOS_FUENTE)
    if not turnos:
        return ResultadoAprende(False, "Sin material para auto-aprender.")
    datos = await _destilar(provider, _material_de_turnos(turnos))
    if not datos or not datos.get("vale", False):
        return ResultadoAprende(False, "No salió una skill que valga (auto).")

    res = await _guardar_con_governor(pool, datos, provenance="auto", creada_por=creada_por)
    # Si pasó el governor, la skill auto NACE en 'stale' (NO se inyecta al chat
    # todavía) y va al GATE del dueño: aprobar→active, rechazar→archived. Así nada
    # auto-generado entra en uso sin el visto bueno humano.
    if res.ok and res.skill_id is not None:
        async with pool.acquire() as con:
            await con.execute("UPDATE skills SET lifecycle='stale' WHERE id=$1", res.skill_id)
        res.requiere_gate = True
        res.mensaje = f"For3s propone una skill nueva: '{res.nombre}'. Espera tu aprobación."
    return res


# ───────────────────────── gate de skills auto-propuestas (P2) ─────────────────────────
async def aprobar_skill(pool, skill_id: int) -> str | None:
    """El dueño APRUEBA una skill auto-propuesta: stale → active (entra en uso).
    Devuelve el nombre de la skill, o None si no existe / no estaba propuesta."""
    async with pool.acquire() as con:
        nombre = await con.fetchval(
            "UPDATE skills SET lifecycle='active', actualizada_at=now() "
            "WHERE id=$1 AND provenance='auto' AND lifecycle='stale' RETURNING nombre",
            skill_id,
        )
    return nombre


async def rechazar_skill(pool, skill_id: int) -> str | None:
    """El dueño RECHAZA una skill auto-propuesta: stale → archived (recuperable,
    nunca se borra). Devuelve el nombre, o None si no aplica."""
    async with pool.acquire() as con:
        nombre = await con.fetchval(
            "UPDATE skills SET lifecycle='archived', actualizada_at=now() "
            "WHERE id=$1 AND provenance='auto' AND lifecycle='stale' RETURNING nombre",
            skill_id,
        )
    return nombre


# ───────────────────────── P3: curación nocturna (reusa H6) ─────────────────────────
# El "curator": de noche, las skills AUTO que no se usan se degradan poco a poco,
# recuperable (nunca hard-delete). Las del USUARIO y las PINNED son intocables.
# Umbrales (mismo espíritu que la Microglía de H6): generoso, no agresivo.
DIAS_ACTIVE_A_STALE = 30  # auto activa sin uso 30d → stale (descansa, ya no se inyecta)
DIAS_STALE_A_ARCHIVED = 90  # auto stale sin uso 90d → archivada (recuperable)


@dataclass
class ResultadoCuracion:
    """Qué movió la curación nocturna (para el log del worker)."""

    a_stale: int = 0
    a_archived: int = 0

    def __str__(self) -> str:
        return f"curación skills: {self.a_stale} →stale, {self.a_archived} →archived"


async def curar_skills(pool, *, confirmar: bool = True) -> ResultadoCuracion:
    """Curación nocturna de skills AUTO sin uso. Reusa la filosofía de H6 (degradar,
    no borrar; recuperable). NUNCA toca skills del usuario ni pinned.

    - active  + provenance='auto' + sin uso + actualizada hace ≥30d  → stale
    - stale   + provenance='auto' + sin uso + actualizada hace ≥90d  → archived

    'sin uso' = veces_usada=0 AND ultimo_uso IS NULL. Una skill que se usó alguna vez
    resiste el archivado (como en H6 lo recuperado resiste el olvido).
    OJO: las propuestas del gate (P2) están en 'stale' PERO son recientes → no caen
    en el corte de 90d, así que la curación no las descarta antes de que el dueño
    decida. confirmar=False = DRY-RUN (solo cuenta). Defensivo."""
    res = ResultadoCuracion()
    try:
        async with pool.acquire() as con:
            if confirmar:
                r1 = await con.execute(
                    "UPDATE skills SET lifecycle='stale', actualizada_at=now() "
                    "WHERE provenance='auto' AND lifecycle='active' AND NOT pinned "
                    "AND veces_usada=0 AND ultimo_uso IS NULL "
                    "AND actualizada_at < now() - make_interval(days => $1)",
                    DIAS_ACTIVE_A_STALE,
                )
                r2 = await con.execute(
                    "UPDATE skills SET lifecycle='archived', actualizada_at=now() "
                    "WHERE provenance='auto' AND lifecycle='stale' AND NOT pinned "
                    "AND veces_usada=0 AND ultimo_uso IS NULL "
                    "AND actualizada_at < now() - make_interval(days => $1)",
                    DIAS_STALE_A_ARCHIVED,
                )
                res.a_stale = int(r1.split()[-1]) if r1 else 0
                res.a_archived = int(r2.split()[-1]) if r2 else 0
            else:  # DRY-RUN: solo cuenta
                res.a_stale = (
                    await con.fetchval(
                        "SELECT count(*) FROM skills WHERE provenance='auto' "
                        "AND lifecycle='active' AND NOT pinned AND veces_usada=0 "
                        "AND ultimo_uso IS NULL "
                        "AND actualizada_at < now() - make_interval(days => $1)",
                        DIAS_ACTIVE_A_STALE,
                    )
                    or 0
                )
                res.a_archived = (
                    await con.fetchval(
                        "SELECT count(*) FROM skills WHERE provenance='auto' "
                        "AND lifecycle='stale' AND NOT pinned AND veces_usada=0 "
                        "AND ultimo_uso IS NULL "
                        "AND actualizada_at < now() - make_interval(days => $1)",
                        DIAS_STALE_A_ARCHIVED,
                    )
                    or 0
                )
    except Exception:  # noqa: BLE001 — la curación nunca debe tumbar el worker
        logger.warning("curación de skills falló (ignoro)", exc_info=True)
    return res
