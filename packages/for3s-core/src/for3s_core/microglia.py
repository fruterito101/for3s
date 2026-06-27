"""For3s OS — Microglía: olvido inteligente de memoria (H6 "SE CUIDA", Nodo 6).

En el cerebro, la microglía poda las conexiones neuronales que ya no se usan. Aquí:
marca para olvido (SOFT-delete) los episodios viejos, poco relevantes y YA consolidados
(su lección ya quedó en el Knowledge Graph). Simbiótica con CLS: solo olvida lo que CLS
ya "exprimió" → la lección sobrevive en el grafo, se archiva el episodio crudo.

⚠️ EL SUBSISTEMA MÁS DELICADO DE H6: es el único que BORRA. Por eso:
  · Sub-paso 8 (ESTE): SOLO evalúa y REPORTA candidatos. NO borra nada (dry-run puro).
  · Sub-paso 9: soft-delete real (recuperable: deleted_at), con doble candado.

REGLA DE ORO: nunca olvidar algo no consolidado · nunca hard-delete (solo soft,
recuperable) · NUNCA tocar el audit chain (Microglía solo toca episodes_events).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger("for3s.microglia")

# Las 3 condiciones para ser candidato a olvido (TODAS deben cumplirse).
DIAS_MINIMO = 30  # más viejo que esto (created_at)
RELEVANCE_MAXIMA = 0.3  # menos relevante que esto
# (la 3ª condición es consolidated_to_kg = true)


@dataclass(frozen=True)
class Candidato:
    """Un episodio candidato a olvido, con el porqué (para revisión humana)."""

    seq: int
    role: str
    preview: str  # primeros chars del contenido (para reconocerlo)
    dias: float  # antigüedad en días
    relevance: float | None


async def evaluar_candidatos(
    pool: asyncpg.Pool,
    session_id: str,
    *,
    dias_minimo: int = DIAS_MINIMO,
    relevance_max: float = RELEVANCE_MAXIMA,
    limite: int = 1000,
) -> list[Candidato]:
    """DRY-RUN PURO: devuelve los episodios candidatos a olvido SIN borrar nada.

    Candidato = cumple LAS TRES condiciones:
      1. created_at más viejo que `dias_minimo`
      2. relevance < `relevance_max` (y NO null)
      3. consolidated_to_kg = true  (su lección ya está en el grafo)
    Y además está vivo (deleted_at IS NULL).

    NO ejecuta ningún DELETE ni UPDATE. Solo SELECT. Para revisar antes del Sub-paso 9.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT seq, role, left(content, 70) AS preview, "
            "       EXTRACT(EPOCH FROM (now() - created_at)) / 86400.0 AS dias, "
            "       relevance "
            "FROM episodes_events "
            "WHERE session_id = $1 "
            "  AND deleted_at IS NULL "
            "  AND consolidated_to_kg = true "
            "  AND relevance IS NOT NULL AND relevance < $2 "
            "  AND created_at < now() - make_interval(days => $3) "
            "ORDER BY relevance ASC, dias DESC "
            "LIMIT $4",
            session_id,
            relevance_max,
            dias_minimo,
            limite,
        )
    candidatos = [
        Candidato(
            seq=r["seq"],
            role=r["role"],
            preview=r["preview"],
            dias=float(r["dias"]),
            relevance=r["relevance"],
        )
        for r in rows
    ]
    logger.info(
        "[microglia] dry-run: %d candidatos a olvido (viejo>%dd, rel<%.2f, consolidado)",
        len(candidatos),
        dias_minimo,
        relevance_max,
    )
    return candidatos


# ===========================================================================
# Sub-paso 9 — SOFT-DELETE REAL (doble candado, recuperable, audita)
# ===========================================================================

MAX_OLVIDO_POR_RUN = 20  # tope duro: nunca olvidar más de N por corrida (anti-bug).
# Conservador a propósito al activar el olvido real (2026-06-20): si algún día
# hay candidatos, se podan de a pocos por noche (recuperables), no masivo de golpe.


@dataclass(frozen=True)
class ResultadoOlvido:
    """Resumen de una corrida de la Microglía."""

    confirmado: bool  # False = dry-run (no borró); True = soft-delete real
    candidatos: int  # cuántos cumplían las 3 condiciones
    olvidados: int  # cuántos se soft-borraron (0 si dry-run)
    seqs: list[int]  # los seq afectados/candidatos


async def olvidar(
    pool: asyncpg.Pool,
    session_id: str,
    *,
    confirmar: bool = False,
    max_por_run: int = MAX_OLVIDO_POR_RUN,
    dias_minimo: int = DIAS_MINIMO,
    relevance_max: float = RELEVANCE_MAXIMA,
) -> ResultadoOlvido:
    """Olvida (SOFT-delete) los episodios candidatos. DOBLE CANDADO:

      · confirmar=False (default) → DRY-RUN: reporta candidatos, NO borra nada.
      · confirmar=True            → soft-delete REAL (deleted_at=now()), recuperable.

    Tope de seguridad: nunca borra más de `max_por_run` en una corrida (si hay más
    candidatos, borra los primeros y avisa — protege contra un borrado masivo por bug).

    ⛔ Solo SOFT-delete (recuperable con recuperar()). NUNCA hard-delete. NUNCA toca
    audit_events. Solo borra lo que evaluar_candidatos aprobó (las 3 condiciones).
    Registra el evento en el audit chain.
    """
    candidatos = await evaluar_candidatos(
        pool,
        session_id,
        dias_minimo=dias_minimo,
        relevance_max=relevance_max,
        limite=max_por_run,
    )
    seqs = [c.seq for c in candidatos]

    if not confirmar:
        logger.info("[microglia] DRY-RUN: %d candidatos (confirmar=False → NO borra)", len(seqs))
        # trazabilidad: el dry-run también deja rastro en audit ("evalué N, no borré")
        try:
            from for3s_core import audit

            await audit.append(
                pool,
                actor="microglia",
                action="microglia_forget_dryrun",
                detail={"session_id": session_id, "candidatos": len(seqs), "seqs": seqs[:100]},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[microglia] audit dryrun falló (no crítico): %s", type(e).__name__)
        return ResultadoOlvido(False, len(candidatos), 0, seqs)

    if not seqs:
        return ResultadoOlvido(True, 0, 0, [])

    # SOFT-delete real (recuperable). Solo episodios que SIGUEN cumpliendo: vivos +
    # consolidados (doble verificación en el propio UPDATE, por si algo cambió).
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE episodes_events SET deleted_at = now() "
            "WHERE session_id = $1 AND seq = ANY($2::int[]) "
            "  AND deleted_at IS NULL AND consolidated_to_kg = true",
            session_id,
            seqs,
        )
    try:
        olvidados = int(result.split()[-1])
    except (ValueError, IndexError):
        olvidados = 0

    # audit (trazabilidad) — NO toca audit_events directamente, usa el append oficial
    try:
        from for3s_core import audit

        await audit.append(
            pool,
            actor="microglia",
            action="microglia_forget",
            detail={"session_id": session_id, "olvidados": olvidados, "seqs": seqs[:100]},
        )
    except Exception as e:  # noqa: BLE001 — audit no debe tumbar la corrida
        logger.warning("[microglia] audit append falló (no crítico): %s", type(e).__name__)

    logger.info("[microglia] soft-delete REAL: %d episodios olvidados (recuperables)", olvidados)
    return ResultadoOlvido(True, len(candidatos), olvidados, seqs)


async def recuperar(pool: asyncpg.Pool, session_id: str, seqs: list[int]) -> int:
    """Revierte un soft-delete (deleted_at = NULL) → el episodio vuelve a la memoria.
    Procedimiento de recuperación por si la Microglía olvidó algo de más. Devuelve
    cuántos recuperó."""
    if not seqs:
        return 0
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE episodes_events SET deleted_at = NULL "
            "WHERE session_id = $1 AND seq = ANY($2::int[])",
            session_id,
            seqs,
        )
    try:
        n = int(result.split()[-1])
    except (ValueError, IndexError):
        n = 0
    logger.info("[microglia] recuperados %d episodios (deleted_at=NULL)", n)
    return n
