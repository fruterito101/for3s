"""For3s OS — CONFIDENCE / metacognición (H10-PLANEA, 2026-06-26).

"Sé cuándo NO sé": For3s evalúa su PROPIA confianza en una respuesta. Si es alta,
responde normal; si es baja, lo DICE / pide aclaración / marca tentativo en vez de
inventar con falsa seguridad. Es el Nodo PFC del cerebro (metacognición).

Diseño LOCKED R6 §6.1.2 (8 señales ponderadas, 5 niveles). Honestidad de ingeniería
(igual criterio que H9): las señales que tienen infra REAL calculan de verdad; las que
no, se marcan `disponible=False` y NO diluyen el score (no inventan señal). Cuando exista
la infra (golden set, plan-then-execute, etc.) se llenan sin tocar el resto.

v1 LOCKED: aplica en la respuesta de chat (conversation.send). Acción en baja confianza =
añadir honestidad (no re-planear aún). DEFENSIVO: si algo falla, devuelve MEDIUM (neutro)
— el confidence NUNCA rompe el turno.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("for3s.confidence")

# Pesos de las 8 señales (R6 §6.1.2 LOCKED).
SIGNAL_WEIGHTS = {
    "llm_self_report": 1.0,
    "tool_success": 2.0,
    "schema_valid": 2.5,
    "cost_accuracy": 1.5,
    "plan_consistency": 2.0,
    "multi_agent_consensus": 3.0,
    "historical": 2.5,
    "rule_eval": 3.0,
}


class ConfidenceLevel(str, Enum):  # noqa: UP042 — str+Enum a propósito (serializa directo, fiel R6)
    HIGH = "high"  # 0.90+
    MED_HIGH = "med_high"  # 0.70-0.89
    MEDIUM = "medium"  # 0.50-0.69
    LOW = "low"  # 0.30-0.49
    CRITICAL = "critical"  # <0.30

    @classmethod
    def from_value(cls, v: float) -> ConfidenceLevel:
        if v >= 0.90:
            return cls.HIGH
        if v >= 0.70:
            return cls.MED_HIGH
        if v >= 0.50:
            return cls.MEDIUM
        if v >= 0.30:
            return cls.LOW
        return cls.CRITICAL


@dataclass(frozen=True)
class Signal:
    """Una señal de confianza: su valor (0-1), si tiene datos reales, y por qué."""

    nombre: str
    valor: float  # 0..1
    disponible: bool  # False = sin infra → NO entra en la agregación
    motivo: str = ""


@dataclass(frozen=True)
class ConfidenceScore:
    valor: float  # 0..1 agregado (solo señales disponibles)
    nivel: ConfidenceLevel
    señales: list[Signal]

    @property
    def baja(self) -> bool:
        """¿Confianza baja? (LOW o CRITICAL → toca ser honesto/tentativo)."""
        return self.nivel in (ConfidenceLevel.LOW, ConfidenceLevel.CRITICAL)


# ───────────────────────── señales con infra REAL ─────────────────────────
# Frases que delatan que el PROPIO modelo no está seguro (señal 1, llm_self_report).
_INSEGURIDAD = (
    "no estoy seguro",
    "no estoy segura",
    "no tengo certeza",
    "creo que",
    "probablemente",
    "podría ser",
    "podria ser",
    "tal vez",
    "quizá",
    "quiza",
    "no sé",
    "no se ",
    "no lo sé",
    "no tengo información",
    "no tengo informacion",
    "no me consta",
    "asumo que",
    "supongo que",
    "no recuerdo",
    "no estoy del todo",
    "puede que",
    "habría que verificar",
    "habria que verificar",
    "no estoy convencido",
)
# Frases de NEGACIÓN HONESTA fuerte (no es inseguridad mala — es buena, pero indica
# que el tema cae fuera de lo conocido → confianza media-baja en "tener la respuesta").
_SIN_DATO = (
    "no tengo registro",
    "no aparece en mi memoria",
    "no hemos hablado",
    "eso no lo trabajamos",
    "no encontré",
    "no encontre",
)


def signal_llm_self_report(respuesta: str) -> Signal:
    """Señal 1: ¿el modelo expresó incertidumbre en SU propia respuesta? (texto).
    Más marcadores de duda → menos confianza. Real y barato (sin 2ª llamada LLM)."""
    t = (respuesta or "").lower()
    if not t.strip():
        return Signal("llm_self_report", 0.3, True, "respuesta vacía")
    hits = sum(1 for f in _INSEGURIDAD if f in t)
    sin_dato = any(f in t for f in _SIN_DATO)
    if hits == 0 and not sin_dato:
        valor = 0.92  # sin marcadores de duda → seguro
    elif sin_dato and hits == 0:
        valor = 0.6  # negación honesta clara (sabe que no sabe)
    else:
        valor = max(0.2, 0.85 - 0.18 * hits)  # cada marcador baja la confianza
    return Signal(
        "llm_self_report",
        round(valor, 3),
        True,
        f"{hits} marcadores de duda" + (" + sin-dato" if sin_dato else ""),
    )


def signal_tool_success(tools_ok: bool | None, hubo_tools: bool) -> Signal:
    """Señal 2: ¿las tools del turno funcionaron? Solo disponible si hubo tools."""
    if not hubo_tools:
        return Signal("tool_success", 0.0, False, "no hubo tools en este turno")
    return Signal(
        "tool_success",
        1.0 if tools_ok else 0.25,
        True,
        "tools ok" if tools_ok else "alguna tool falló",
    )


def signal_schema_valid(schema_ok: bool | None) -> Signal:
    """Señal 3: ¿la salida estructurada parseó bien? None = no aplicaba (chat libre)."""
    if schema_ok is None:
        return Signal("schema_valid", 0.0, False, "sin salida estructurada")
    return Signal(
        "schema_valid",
        1.0 if schema_ok else 0.2,
        True,
        "schema válido" if schema_ok else "schema inválido",
    )


def signal_historical(tasa_error_reciente: float | None) -> Signal:
    """Señal 7: salud histórica reciente (del audit). tasa_error alta → menos confianza.
    None = sin datos suficientes → no disponible."""
    if tasa_error_reciente is None:
        return Signal("historical", 0.0, False, "sin histórico suficiente")
    valor = max(0.0, 1.0 - tasa_error_reciente)
    return Signal(
        "historical", round(valor, 3), True, f"tasa error reciente {tasa_error_reciente:.0%}"
    )


# ───────────────────────── señales SIN infra (neutras, honestas) ─────────────────────────
def _neutra(nombre: str, motivo: str) -> Signal:
    return Signal(nombre, 0.0, False, motivo)


def señales_neutras_pendientes() -> list[Signal]:
    """Las 4 señales que aún no tienen infra (deuda documentada). NO diluyen el score."""
    return [
        _neutra("cost_accuracy", "no medimos estimado vs real por turno (deuda)"),
        _neutra("plan_consistency", "sin plan-then-execute formal (deuda)"),
        _neutra("multi_agent_consensus", "solo aplica cuando corre el equipo (H8)"),
        _neutra("rule_eval", "requiere golden set formal (= deuda H9-D3)"),
    ]


# ───────────────────────── agregación ─────────────────────────
def agregar(señales: list[Signal]) -> ConfidenceScore:
    """Score ponderado SOLO sobre las señales disponibles (las neutras no cuentan).
    Si ninguna está disponible → MEDIUM (0.6, neutro honesto). DEFENSIVO.

    ⭐ REGLA DE TOPE (calibración v1): si el PROPIO modelo expresó inseguridad clara
    (llm_self_report bajo), eso MANDA — es la señal más directa de "no sé". No dejamos
    que el histórico general (que mide otra cosa) la tape. El score no puede superar el
    techo que marca la auto-evaluación del modelo."""
    try:
        disp = [s for s in señales if s.disponible]
        if not disp:
            return ConfidenceScore(0.6, ConfidenceLevel.MEDIUM, señales)
        num = sum(s.valor * SIGNAL_WEIGHTS.get(s.nombre, 1.0) for s in disp)
        den = sum(SIGNAL_WEIGHTS.get(s.nombre, 1.0) for s in disp)
        valor = num / den if den else 0.6
        # tope por auto-reporte: si el modelo dijo que dudaba, el score no lo ignora.
        # La auto-evaluación del modelo (¿yo sé esto?) manda sobre el histórico general.
        self_rep = next((s for s in disp if s.nombre == "llm_self_report"), None)
        if self_rep is not None and self_rep.valor < 0.65:
            valor = min(valor, self_rep.valor)  # su propia duda es el techo del score
        return ConfidenceScore(round(valor, 3), ConfidenceLevel.from_value(valor), señales)
    except Exception:  # noqa: BLE001 — el confidence nunca rompe el turno
        logger.warning("agregar confianza falló — devuelvo MEDIUM", exc_info=True)
        return ConfidenceScore(0.6, ConfidenceLevel.MEDIUM, señales)


# ───────────────────────── helper de alto nivel para chat (v1) ─────────────────────────
async def evaluar_respuesta_chat(pool, *, respuesta: str, session_id: str) -> ConfidenceScore:
    """Calcula el confidence de una respuesta de CHAT (conversation.send). v1: usa las
    señales con infra en este flujo (llm_self_report del texto + historical del audit) +
    las neutras documentadas. Defensivo: nunca rompe."""
    señales: list[Signal] = [signal_llm_self_report(respuesta)]
    # historical: tasa de mensajes 'message_out' con error/vacío en las últimas horas
    tasa = None
    try:
        async with pool.acquire() as con:
            total = (
                await con.fetchval(
                    "SELECT count(*) FROM episodes_events WHERE role='assistant' "
                    "AND created_at >= now()-interval '24 hours' AND deleted_at IS NULL"
                )
                or 0
            )
            vacios = (
                await con.fetchval(
                    "SELECT count(*) FROM episodes_events WHERE role='assistant' "
                    "AND created_at >= now()-interval '24 hours' AND deleted_at IS NULL "
                    "AND (content IS NULL OR length(trim(content)) < 2)"
                )
                or 0
            )
        if total >= 5:  # con poca señal no aporta
            tasa = vacios / total
    except Exception:  # noqa: BLE001
        tasa = None
    señales.append(signal_historical(tasa))
    # en chat puro no hay tools ni schema → neutras (honesto)
    señales.append(signal_tool_success(None, hubo_tools=False))
    señales.append(signal_schema_valid(None))
    señales.extend(señales_neutras_pendientes())
    score = agregar(señales)
    # audit ligero (reusa la cadena) — defensivo
    try:
        from for3s_core import audit

        await audit.append(
            pool,
            actor="for3s",
            action="confidence_calculated",
            detail={"session": session_id, "valor": score.valor, "nivel": score.nivel.value},
        )
    except Exception:  # noqa: BLE001
        pass
    return score


# Nota para el caller (conversation.send): si score.baja, inyectar una NOTA de honestidad
# al contexto ANTES de generar, o post-procesar la respuesta para marcarla tentativa.
NOTA_BAJA_CONFIANZA = (
    "NOTA DE METACOGNICIÓN (H10): tu confianza en responder esto es BAJA. Sé HONESTO: "
    "si no tienes base sólida, DILO ('no estoy seguro de X'), ofrece verificar o pide la "
    "aclaración que te falta. NUNCA inventes con falsa seguridad."
)
