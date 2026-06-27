"""For3s OS — STATUS por hilo (AI4 auto-retomar, 2026-06-23).

Cada hilo (persona×tema, del #6/AI2) tiene un STATUS corto curado ("en qué
quedamos"). Se REGENERA de noche (junto con H6/CLS, cero costo de día) y se INYECTA
al contexto cuando la persona retoma ese hilo tras inactividad. Es el "RETOMAR.md"
automático por hilo.

OAUTH-SAFE: la instrucción de resumen va en el USER message, system="" (regla 429).
DEFENSIVO: si generar/leer falla, NO rompe nada — el flujo normal (12 turnos +
memoria) sigue igual.
"""

from __future__ import annotations

import logging
import os

from for3s_core import memory

logger = logging.getLogger("for3s.hilo_status")

# Modelo para resumir (reusa el de CLS por defecto — sonnet-4-6, configurable).
STATUS_MODEL = os.environ.get("FOR3S_CLS_MODEL", "claude-sonnet-4-6").strip()

# Horas de inactividad tras las que conviene inyectar el STATUS al retomar.
HORAS_RETOMAR = 3
# Cuántos turnos recientes se resumen para armar el STATUS.
TURNOS_RESUMEN = 16
# Mínimo de turnos para que valga la pena tener STATUS (hilos muy nuevos no).
MIN_TURNOS = 4

_INSTRUCCION = (
    "Eres el sistema de continuidad de For3s. Resume EN QUÉ QUEDÓ esta conversación "
    "para retomarla después, en máximo 5 líneas, formato:\n"
    "Tema: <de qué trata>\nEn qué quedamos: <último estado real>\n"
    "Próximo paso: <lo siguiente, si se ve>\n"
    "Sé concreto y breve. NO inventes; si no hay próximo paso claro, omite esa línea.\n\n"
    "CONVERSACIÓN (turnos recientes):\n"
)


async def get_status(pool, session_id: str) -> tuple[str, object] | None:
    """Devuelve (texto, actualizado_at) del STATUS de un hilo, o None si no hay."""
    try:
        async with pool.acquire() as con:
            r = await con.fetchrow(
                "SELECT texto, actualizado_at FROM hilo_status WHERE session_id = $1",
                session_id,
            )
        return (r["texto"], r["actualizado_at"]) if r else None
    except Exception:  # noqa: BLE001 — leer STATUS nunca rompe el flujo
        logger.warning("error leyendo hilo_status (ignoro)", exc_info=True)
        return None


async def guardar_status(pool, session_id: str, texto: str) -> None:
    """Upsert del STATUS de un hilo."""
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO hilo_status (session_id, texto, actualizado_at) "
            "VALUES ($1, $2, now()) "
            "ON CONFLICT (session_id) DO UPDATE "
            "SET texto = $2, actualizado_at = now()",
            session_id,
            texto,
        )


async def debe_inyectar(pool, session_id: str) -> str | None:
    """¿Conviene inyectar el STATUS al retomar? Devuelve el texto si el último turno
    del hilo fue hace >= HORAS_RETOMAR (la persona vuelve tras un hueco) y hay STATUS.
    Si está en conversación activa (turno reciente), devuelve None (los 12 turnos
    bastan). DEFENSIVO: ante error, None."""
    try:
        async with pool.acquire() as con:
            ult = await con.fetchval(
                "SELECT max(created_at) FROM episodes_events WHERE session_id = $1",
                session_id,
            )
            if ult is None:
                return None
            horas = await con.fetchval(
                "SELECT EXTRACT(EPOCH FROM (now() - $1))/3600",
                ult,
            )
        if horas is None or float(horas) < HORAS_RETOMAR:
            return None  # conversación activa → no estorbar
        st = await get_status(pool, session_id)
        return st[0] if st else None
    except Exception:  # noqa: BLE001
        logger.warning("error en debe_inyectar (ignoro)", exc_info=True)
        return None


async def generar_status(pool, session_id: str, *, provider=None) -> str | None:
    """Genera (y guarda) el STATUS de UN hilo resumiendo sus turnos recientes con el
    LLM. Pensado para correr DE NOCHE (junto con H6). Devuelve el texto, o None si el
    hilo es muy nuevo / falla. OAUTH-SAFE + DEFENSIVO (un STATUS fallido no tumba el
    ciclo nocturno)."""
    try:
        turns = await memory.load_history(pool, session_id, last_n=TURNOS_RESUMEN)
        if len(turns) < MIN_TURNOS:
            return None  # hilo muy nuevo, no vale la pena
        cuerpo = "\n".join(
            f"{'Usuario' if t.role == 'user' else 'For3s'}: {(t.content or '').strip()[:300]}"
            for t in turns
        )
        prompt = _INSTRUCCION + cuerpo
        if provider is None:
            from for3s_core.config import load_settings
            from for3s_core.llm import ClaudeProvider

            s = load_settings()
            provider = ClaudeProvider(token=s.anthropic_token, oauth=s.is_oauth, model=STATUS_MODEL)
        import asyncio

        resp = await asyncio.to_thread(
            provider.complete,
            prompt,
            system="",
            max_tokens=200,
        )
        texto = (resp.text or "").strip()
        if not texto:
            return None
        await guardar_status(pool, session_id, texto)
        logger.info("[hilo_status] STATUS generado para %s (%d chars)", session_id, len(texto))
        return texto
    except Exception:  # noqa: BLE001 — un STATUS fallido NUNCA tumba el ciclo nocturno
        logger.warning("[hilo_status] no pude generar STATUS de %s", session_id, exc_info=True)
        return None


async def hilos_activos(pool, *, dias: int = 7) -> list[str]:
    """session_ids con actividad en los últimos N días (los que vale resumir de
    noche). Evita resumir hilos muertos."""
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT DISTINCT session_id FROM episodes_events "
            "WHERE created_at > now() - ($1 || ' days')::interval "
            "AND deleted_at IS NULL",
            str(dias),
        )
    return [r["session_id"] for r in rows]
