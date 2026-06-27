# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — registro de modelos LLM seleccionables (cimiento de H7, 2026-06-22).

Idea (el dueño): For3s tiene su propio `/model` (como Claude Code) — al recibir el token,
VERIFICA qué modelos responden de verdad, los lista, y se eligió cuál usa el bot.
Esto define el "universo de modelos permitidos". H7 (enrutamiento automático) está
BLOQUEADO por ahora — este módulo es solo el registro + selección manual (el /model
mockeado). El enrutamiento Haiku/Sonnet/Opus se construye después, dentro de esta lista.

Verificado 2026-06-22: el token OAuth de el dueño da acceso a los 3 (haiku/sonnet/opus).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("for3s.modelos")


@dataclass(frozen=True)
class ModeloInfo:
    """Un modelo del catálogo: id de API + cómo se presenta (estilo Claude Code)."""

    id: str  # string exacto de la API (claude-sonnet-4-6)
    nombre: str  # nombre corto para mostrar (Sonnet)
    desc: str  # descripción del rol (Efficient for routine tasks)
    rol: str  # rapido | routine | complejo — para el futuro H7


# CATÁLOGO de candidatos (los que el /model de Claude Code muestra + conocidos).
# La verificación con el token filtra cuáles SÍ responden → DISPONIBLES.
CATALOGO: list[ModeloInfo] = [
    ModeloInfo("claude-haiku-4-5", "Haiku", "El más rápido, respuestas simples", "rapido"),
    ModeloInfo("claude-sonnet-4-6", "Sonnet", "Eficiente, tareas de rutina (default)", "routine"),
    ModeloInfo("claude-opus-4-8", "Opus", "El más capaz, tareas complejas", "complejo"),
]

MODELO_DEFAULT = "claude-sonnet-4-6"  # el que usa el bot hoy

# clave en sessions.meta donde se guarda la selección (de la sesión owner)
_META_KEY = "modelo_seleccionado"


def info_de(model_id: str) -> ModeloInfo | None:
    """Devuelve la ModeloInfo de un id, o None si no está en el catálogo."""
    for m in CATALOGO:
        if m.id == model_id:
            return m
    return None


async def verificar_disponibles(
    token: str,
    oauth: bool,
    *,
    espaciar_seg: float = 22.0,
) -> list[str]:
    """Pinguea cada modelo del catálogo con el token y devuelve los que RESPONDEN.
    Espaciado para no topar el rate-limit instantáneo del OAuth. Pensado para correr
    al arranque (1 vez) o bajo demanda, NO en cada turno. Defensiva por modelo."""
    import asyncio

    from for3s_core.llm import ClaudeProvider

    disponibles: list[str] = []
    for i, m in enumerate(CATALOGO):
        if i > 0 and espaciar_seg > 0:
            await asyncio.sleep(espaciar_seg)
        prov = ClaudeProvider(token=token, oauth=oauth, model=m.id)
        try:
            await asyncio.to_thread(prov.complete, "ok", system="", max_tokens=5)
            disponibles.append(m.id)
            logger.info("[modelos] disponible: %s", m.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[modelos] NO disponible: %s (%s)", m.id, type(e).__name__)
    return disponibles


async def get_seleccionado(pool, session_id: str) -> str:
    """Modelo que se eligió (de sessions.meta), o el default si no eligió."""
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                f"SELECT meta -> '{_META_KEY}' AS m FROM sessions WHERE id = $1", session_id
            )
        import json

        if raw:
            val = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(val, str) and info_de(val):
                return val
    except Exception:  # noqa: BLE001 — si falla, default seguro
        pass
    return MODELO_DEFAULT


async def set_seleccionado(pool, session_id: str, model_id: str) -> bool:
    """Guarda el modelo elegido por el dueño en sessions.meta. Valida que esté en el
    catálogo (no se guarda basura). Devuelve True si guardó."""
    if not info_de(model_id):
        return False
    import json

    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE sessions SET meta = jsonb_set(meta, '{{{_META_KEY}}}', $2::jsonb) "
            "WHERE id = $1",
            session_id,
            json.dumps(model_id),
        )
    logger.info("[modelos] seleccionado: %s (sesión %s)", model_id, session_id)
    return True
