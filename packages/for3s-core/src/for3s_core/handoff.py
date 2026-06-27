"""For3s OS — HANDOFF / audit trail del equipo multi-agente (AI3, 2026-06-23).

Audit trail DB-backed del equipo multi-agente. Cada corrida del equipo (H8
correr_equipo) queda REGISTRADA: qué se pidió y qué devolvió CADA specialist (texto
completo). Antes se perdía en RAM.

PRINCIPIO DE SEPARACIÓN DE ESCRITURA: el registro lo escribe el HUB/coordinador
(esta función), NO los specialists. Los specialists solo producen su reporte; el
coordinador es quien persiste. Semilla de la separación de roles del gate (AI3
parte 2 / apartado E).

DEFENSIVO: registrar la corrida NUNCA debe romper la entrega del informe al usuario.
El caller envuelve esto en try/except; aquí además es transaccional (o todo o nada).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("for3s.handoff")


async def registrar_corrida(
    pool, *, session_id: str, telegram_user_id: int | None, tarea: str,
    equipo, informe: str,
) -> int | None:
    """Persiste una corrida del equipo + el reporte de cada specialist.

    equipo: ResultadoEquipo (de multiagente.correr_equipo) — tiene familia, reportes
        (list[ResultadoSpecialist]), n_ok, segundos_total, costo (PresupuestoCorrida).
    informe: la síntesis final entregada al usuario.

    Devuelve el id de la corrida, o None si algo falla (defensivo — el audit es
    secundario al servicio). TRANSACCIONAL: corrida + reportes se escriben juntos."""
    try:
        costo = getattr(equipo, "costo", None)
        tin = getattr(costo, "tokens_in", 0) if costo else 0
        tout = getattr(costo, "tokens_out", 0) if costo else 0
        reportes = getattr(equipo, "reportes", []) or []
        async with pool.acquire() as con:
            async with con.transaction():
                corrida_id = await con.fetchval(
                    "INSERT INTO corridas_equipo "
                    "(session_id, telegram_user_id, tarea, familia, n_specialists, "
                    " n_ok, segundos, tokens_in, tokens_out, informe) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
                    session_id, telegram_user_id, tarea, equipo.familia,
                    len(reportes), equipo.n_ok, equipo.segundos_total,
                    tin, tout, informe,
                )
                for r in reportes:
                    await con.execute(
                        "INSERT INTO corrida_reportes "
                        "(corrida_id, specialist, ok, tokens_in, tokens_out, "
                        " segundos, texto) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                        corrida_id, r.nombre, r.ok, r.tokens_in, r.tokens_out,
                        r.segundos, r.texto,
                    )
        logger.info("[handoff] corrida=%s registrada (%d specialists, %d ok)",
                    corrida_id, len(reportes), equipo.n_ok)
        return corrida_id
    except Exception:  # noqa: BLE001 — el audit NUNCA tumba la entrega del informe
        logger.warning("[handoff] no pude registrar la corrida (no crítico)",
                       exc_info=True)
        return None


async def ultimas_corridas(pool, session_id: str, *, limite: int = 5) -> list[dict]:
    """Devuelve las últimas corridas del equipo de un hilo (para 'qué analizó el
    equipo'). Solo metadatos + tarea; el detalle por specialist se consulta aparte."""
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT id, tarea, familia, n_ok, n_specialists, segundos, creado_at "
            "FROM corridas_equipo WHERE session_id = $1 "
            "ORDER BY creado_at DESC LIMIT $2",
            session_id, limite,
        )
    return [dict(r) for r in rows]
