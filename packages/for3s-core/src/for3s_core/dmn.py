"""For3s OS — DMN "SUEÑA" (H9, 2026-06-25): trabaja solo cuando está idle.

El DMN (Default Mode Network, Nodo 6) es el "modo por defecto del cerebro": cuando
nadie usa el sistema, corre tasks en background. Dos clases (R5 §2):
  • HOUSEKEEPING (5): se mantiene solo — bajo riesgo, AUTO-APLICA, outcome medible.
  • GENERATIVA (3): se mejora solo — alto riesgo, pasa por el GOVERNOR (H11) + gate.
Junto con las Skills (Nodo 4, H10-12) forma el Pilar 3 (autonomía generativa), ambos
gobernados por el MISMO governor.

H9-a = ESTE MOTOR (idle detection + scheduler + registro de corridas + estado/kill
switch). Las 8 tasks se registran en _TASKS (las llenan H9-b housekeeping / H9-c
generativas). Plan: Cuerpo/H9_SUENA_Plan_Maestro_DMN.md.

DEFENSIVO: una task que falla se registra y NO tumba al resto ni al worker.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC

logger = logging.getLogger("for3s.dmn")

WORKSPACE_DEFAULT = "default"

# Idle = sin actividad (ningún turno) en al menos estos minutos. El DMN de DÍA solo
# despierta si el sistema lleva un rato sin uso (no molesta mientras alguien escribe).
IDLE_MIN = int(os.environ.get("FOR3S_DMN_IDLE_MIN", "15"))

# Kill switch de emergencia por entorno (manda sobre la BD, como el governor).
ENV_DMN_OFF = "FOR3S_DMN_OFF"

CLASE_HOUSEKEEPING = "housekeeping"
CLASE_GENERATIVA = "generativa"


@dataclass
class DMNTaskResult:
    """Resultado de la action de una task (R5): qué produjo, cuánto costó."""

    outcome: dict = field(default_factory=dict)  # métrica medible (embeddings_created, ...)
    costo_usd: float = 0.0
    motivo: str = ""


@dataclass(frozen=True)
class DMNTask:
    """Una task del DMN: trigger (¿vale correr?) + action (hace el trabajo)."""

    nombre: str
    clase: str                                   # housekeeping | generativa
    trigger: Callable[..., Awaitable[bool]]      # async (pool, ws) -> bool
    action: Callable[..., Awaitable[DMNTaskResult]]  # async (pool, ws) -> DMNTaskResult
    solo_noche: bool = False                      # True = no corre en idle de día (pesada)


# Registro de tasks. H9-b (housekeeping) y H9-c (generativas) lo llenan vía registrar().
_TASKS: dict[str, DMNTask] = {}


def registrar(task: DMNTask) -> None:
    """Registra una task en el motor (idempotente por nombre)."""
    _TASKS[task.nombre] = task


def tasks_registradas() -> list[DMNTask]:
    return list(_TASKS.values())


# ───────────────────────── idle detection ─────────────────────────
async def minutos_idle(pool, *, workspace: str = WORKSPACE_DEFAULT) -> float | None:
    """Minutos desde la última actividad (último turno de chat). None si no hay datos.
    Reusa created_at de episodes_events (no necesita columna nueva)."""
    try:
        async with pool.acquire() as con:
            ultimo = await con.fetchval(
                "SELECT max(created_at) FROM episodes_events WHERE deleted_at IS NULL")
        if ultimo is None:
            return None
        from datetime import datetime
        ahora = datetime.now(UTC)
        dt = ultimo if ultimo.tzinfo else ultimo.replace(tzinfo=UTC)
        return (ahora - dt).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        logger.warning("no pude medir idle (ignoro)", exc_info=True)
        return None


async def esta_idle(pool, *, minimo: int = IDLE_MIN,
                    workspace: str = WORKSPACE_DEFAULT) -> bool:
    """True si el sistema lleva ≥ `minimo` minutos sin actividad."""
    m = await minutos_idle(pool, workspace=workspace)
    return m is not None and m >= minimo


# ───────────────────────── estado / kill switch ─────────────────────────
async def _clases_activas(pool, *, workspace: str = WORKSPACE_DEFAULT) -> tuple[bool, bool]:
    """(housekeeping_on, generativas_on). El flag de entorno FOR3S_DMN_OFF apaga TODO.
    Fail-closed: si no puede leer, asume housekeeping ON / generativas OFF (default seguro)."""
    if os.environ.get(ENV_DMN_OFF, "").strip().lower() in ("1", "true", "yes", "on"):
        return (False, False)
    try:
        async with pool.acquire() as con:
            r = await con.fetchrow(
                "SELECT housekeeping_on, generativas_on FROM dmn_estado WHERE workspace=$1",
                workspace)
        if r is None:
            return (True, False)
        return (bool(r["housekeeping_on"]), bool(r["generativas_on"]))
    except Exception:  # noqa: BLE001
        logger.warning("no pude leer estado DMN — asumo housekeeping ON / generativas OFF",
                       exc_info=True)
        return (True, False)


async def set_clase(pool, clase: str, on: bool, *, por: int | None = None,
                    motivo: str = "", workspace: str = WORKSPACE_DEFAULT) -> None:
    """Enciende/apaga una clase de tasks (housekeeping|generativa). Solo el dueño."""
    col = "housekeeping_on" if clase == CLASE_HOUSEKEEPING else "generativas_on"
    async with pool.acquire() as con:
        await con.execute(
            f"INSERT INTO dmn_estado (workspace, {col}, cambiado_por, cambiado_at, motivo) "  # noqa: S608
            "VALUES ($1,$2,$3,now(),$4) "
            f"ON CONFLICT (workspace) DO UPDATE SET {col}=$2, cambiado_por=$3, "
            "cambiado_at=now(), motivo=$4",
            workspace, on, por, (motivo or "")[:200])
    logger.info("[dmn] %s_on=%s por=%s", col, on, por)


# ───────────────────────── registro de corridas ─────────────────────────
async def _registrar_corrida(pool, task: DMNTask, *, trigger_ok: bool, corrio: bool,
                             res: DMNTaskResult | None, ms: int,
                             workspace: str = WORKSPACE_DEFAULT) -> None:
    """Deja constancia de una corrida (append-only, base del ROI). Defensivo."""
    import json
    try:
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO dmn_corridas (workspace, task, clase, trigger_ok, corrio, "
                " outcome, costo_usd, ms, motivo) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                workspace, task.nombre, task.clase, trigger_ok, corrio,
                json.dumps(res.outcome if res else {}),
                (res.costo_usd if res else 0.0), ms, (res.motivo if res else "")[:500])
    except Exception:  # noqa: BLE001 — el registro nunca rompe la corrida
        logger.warning("no pude registrar corrida DMN de %s", task.nombre, exc_info=True)


# ───────────────────────── el ciclo del DMN ─────────────────────────
@dataclass
class DMNRunReport:
    """Resumen de un ciclo del DMN (para el log del worker / verificación)."""

    idle_min: float | None
    evaluadas: int = 0
    corridas: int = 0
    saltadas: int = 0
    detalle: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        idle = f"{self.idle_min:.0f}m" if self.idle_min is not None else "?"
        return (f"DMN ciclo: idle={idle} · evaluadas={self.evaluadas} "
                f"corridas={self.corridas} saltadas={self.saltadas}")


async def correr_ciclo(pool, *, solo_noche: bool = False, forzar: bool = False,
                       workspace: str = WORKSPACE_DEFAULT) -> DMNRunReport:
    """Corre UN ciclo del DMN: para cada task elegible (clase ON + trigger OK),
    ejecuta su action y registra el resultado.

    solo_noche: si True, incluye también las tasks pesadas (solo_noche=True). De día
                (idle oportunista) se omiten las pesadas.
    forzar:     ignora el chequeo de idle (para /dmn correr manual y tests).

    Defensivo: una task que revienta se registra y el ciclo sigue."""
    idle = await minutos_idle(pool, workspace=workspace)
    rep = DMNRunReport(idle_min=idle)

    if not forzar and not (idle is not None and idle >= IDLE_MIN):
        rep.detalle.append(f"no-idle ({idle}m < {IDLE_MIN}m) — no corro")
        return rep

    hk_on, gen_on = await _clases_activas(pool, workspace=workspace)

    for task in _TASKS.values():
        # ¿la clase de esta task está encendida?
        clase_on = hk_on if task.clase == CLASE_HOUSEKEEPING else gen_on
        if not clase_on:
            rep.saltadas += 1
            continue
        # ¿es pesada y estamos en idle de día (no solo_noche)?
        if task.solo_noche and not solo_noche:
            rep.saltadas += 1
            continue

        rep.evaluadas += 1
        t0 = time.monotonic()
        trigger_ok = False
        res: DMNTaskResult | None = None
        corrio = False
        try:
            trigger_ok = await task.trigger(pool, workspace)
            if trigger_ok:
                res = await task.action(pool, workspace)
                corrio = True
        except Exception as e:  # noqa: BLE001 — una task no tumba al resto
            res = DMNTaskResult(motivo=f"error: {type(e).__name__}")
            logger.exception("[dmn] task %s falló", task.nombre)
        ms = int((time.monotonic() - t0) * 1000)
        await _registrar_corrida(pool, task, trigger_ok=trigger_ok, corrio=corrio,
                                 res=res, ms=ms, workspace=workspace)
        if corrio:
            rep.corridas += 1
            rep.detalle.append(f"{task.nombre}: {res.outcome if res else {}}")
        else:
            rep.detalle.append(f"{task.nombre}: trigger={trigger_ok} (no corrió)")

    logger.info("[dmn] %s", rep)
    return rep


# ───────────────────────── reporte de estado (para /dmn status) ─────────────────────────
@dataclass(frozen=True)
class DMNStatus:
    idle_min: float | None
    housekeeping_on: bool
    generativas_on: bool
    tasks_registradas: int
    corridas_hoy: int


async def status(pool, *, workspace: str = WORKSPACE_DEFAULT) -> DMNStatus:
    """Foto del DMN para el comando /dmn status. Best-effort."""
    idle = await minutos_idle(pool, workspace=workspace)
    hk_on, gen_on = await _clases_activas(pool, workspace=workspace)
    corridas_hoy = 0
    try:
        async with pool.acquire() as con:
            corridas_hoy = await con.fetchval(
                "SELECT count(*) FROM dmn_corridas WHERE corrio=true "
                "AND creado_at >= date_trunc('day', now())") or 0
    except Exception:  # noqa: BLE001
        pass
    return DMNStatus(idle, hk_on, gen_on, len(_TASKS), corridas_hoy)


# ───────────── propuestas generativas (H9-c) ─────────────
# Las tasks generativas (worker, sin Telegram) dejan propuestas aquí; el dueño las
# resuelve desde el bot (/dmn propuestas). NADA se auto-aplica.
@dataclass(frozen=True)
class DMNPropuesta:
    id: int
    task: str
    tipo: str
    titulo: str
    contenido: str
    creada_at: object


async def propuestas_pendientes(pool, *, limite: int = 10,
                                workspace: str = WORKSPACE_DEFAULT) -> list[DMNPropuesta]:
    """Propuestas generativas que esperan decisión del dueño. Defensivo."""
    try:
        async with pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, task, tipo, titulo, contenido, creada_at FROM dmn_propuestas "
                "WHERE estado='pendiente' AND workspace=$1 ORDER BY creada_at DESC LIMIT $2",
                workspace, limite)
        return [DMNPropuesta(r["id"], r["task"], r["tipo"], r["titulo"],
                             r["contenido"], r["creada_at"]) for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning("no pude leer propuestas DMN", exc_info=True)
        return []


async def resolver_propuesta(pool, prop_id: int, *, aprobar: bool,
                             por: int | None = None) -> str | None:
    """El dueño aprueba/descarta una propuesta. Devuelve el título, o None si no aplica.
    v1: aprobar = marcarla aprobada (queda como registro/idea validada — aplicarla es
    trabajo manual o futuro AC3). descartar = archivarla. Nunca auto-ejecuta."""
    estado = "aprobada" if aprobar else "descartada"
    try:
        async with pool.acquire() as con:
            return await con.fetchval(
                "UPDATE dmn_propuestas SET estado=$2, resuelta_por=$3, resuelta_at=now() "
                "WHERE id=$1 AND estado='pendiente' RETURNING titulo",
                prop_id, estado, por)
    except Exception:  # noqa: BLE001
        return None


# ───────────── ROI tracking (H9-d, R5 §6): cada task se gana su lugar ─────────────
# "Una task puede correr 1000 veces, gastar $$ y no aportar — y nadie lo sabría."
# Solución: medir, por task, cuánto CORRIÓ y cuánto COSTÓ en una ventana, y dar una
# recomendación simple (keep|revisar). El "valor" fino por task (eval, hit-rate, etc.)
# es R5 §6 completo; v1 da la base medible sobre dmn_corridas (datos reales ya).
@dataclass(frozen=True)
class TaskROI:
    task: str
    clase: str
    corridas: int          # veces que la action se ejecutó (corrio=true)
    evaluadas: int         # veces que el trigger se evaluó
    costo_total: float     # USD acumulado en la ventana
    ultima: object         # fecha de la última corrida
    recomendacion: str     # keep | revisar (gastó y nunca corrió de verdad) | sin-datos


async def roi_por_task(pool, *, dias: int = 30,
                       workspace: str = WORKSPACE_DEFAULT) -> list[TaskROI]:
    """ROI de cada task en los últimos `dias`. Sobre dmn_corridas (real). Defensivo.

    Recomendación v1 (conservadora, solo SUGIERE — nunca apaga sola):
      • 'revisar'   → gastó dinero (costo>0) pero la action casi nunca corrió útil,
                      o corre mucho sin producir outcome → candidata a apagar.
      • 'keep'      → corre y aporta (o es $0, siempre vale).
      • 'sin-datos' → aún no hay corridas en la ventana."""
    out: list[TaskROI] = []
    try:
        async with pool.acquire() as con:
            rows = await con.fetch(
                "SELECT task, max(clase) AS clase, "
                "  count(*) FILTER (WHERE corrio) AS corridas, "
                "  count(*) AS evaluadas, "
                "  coalesce(sum(costo_usd),0) AS costo, max(creado_at) AS ultima "
                "FROM dmn_corridas "
                "WHERE workspace=$1 AND creado_at >= now() - make_interval(days => $2) "
                "GROUP BY task ORDER BY costo DESC, corridas DESC",
                workspace, dias)
        for r in rows:
            corridas = r["corridas"] or 0
            costo = float(r["costo"] or 0)
            if r["evaluadas"] == 0:
                rec = "sin-datos"
            elif costo > 0 and corridas == 0:
                rec = "revisar"   # gastó (se intentó) pero nunca produjo → ¿vale?
            else:
                rec = "keep"
            out.append(TaskROI(r["task"], r["clase"] or "", corridas, r["evaluadas"] or 0,
                               costo, r["ultima"], rec))
    except Exception:  # noqa: BLE001
        logger.warning("no pude calcular ROI del DMN", exc_info=True)
    return out
