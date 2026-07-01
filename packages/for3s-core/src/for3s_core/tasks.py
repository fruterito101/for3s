# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — scheduler de jobs en segundo plano (H6 "SE CUIDA").

Worker de Arq que corre los jobs nocturnos de H6:
  · CLS (consolidación episodios→KG)  — 02:00 México (08:00 UTC)
  · Microglía (olvido inteligente)     — 03:00 México (09:00 UTC), DESPUÉS de CLS

⚠️ AISLAMIENTO DE VALKEY (crítico):
  El cache de lecturas de GitHub (cache.py) usa Valkey en la **db 0** (default,
  con prefijo "for3s:gh"). El scheduler usa una **db lógica separada (db 1)** para
  no pisar nunca las keys del cache. Son cajones distintos del mismo Valkey.

⚠️ ZONA HORARIA: el servidor corre en UTC. Para 2 AM / 3 AM hora de México (UTC-6)
  → 8:00 / 9:00 UTC. Si el server cambiara de TZ, recalcular HORA_*_UTC.

⚠️ DOBLE CANDADO Microglía: por defecto DRY-RUN (FOR3S_MICROGLIA_CONFIRMAR=false),
  solo REPORTA qué podaría, NO borra. Para activar el borrado nocturno real, poner
  FOR3S_MICROGLIA_CONFIRMAR=true en el .env y reiniciar el worker.

Worker: systemd unit `for3s-worker.service` → uv run arq for3s_core.tasks.WorkerSettings
"""

from __future__ import annotations

import asyncio
import logging
import os

from arq import cron
from arq.connections import RedisSettings

from for3s_core.tiempo import ahora_local

logger = logging.getLogger("for3s.worker")

# --- Aislamiento de Valkey -------------------------------------------------
# Host/puerto por entorno (default localhost) → en docker-compose apunta al
# servicio 'valkey'; en el server local sigue siendo 127.0.0.1. No rompe nada.
VALKEY_HOST = os.environ.get("VALKEY_HOST", "127.0.0.1").strip()
VALKEY_PORT = int(os.environ.get("VALKEY_PORT", "6379"))
VALKEY_DB_SCHEDULER = 1  # ⚠️ NO usar 0 (es del cache de GitHub)

REDIS_SETTINGS = RedisSettings(
    host=VALKEY_HOST,
    port=VALKEY_PORT,
    database=VALKEY_DB_SCHEDULER,
)

# --- Config de los jobs nocturnos ------------------------------------------
# El worker NO hereda el .env por systemd (igual que el bot). Cargarlo aquí, al
# importar, para que las variables FOR3S_* se lean (si no, MICROGLIA_CONFIRMAR
# quedaría siempre en su default 'false' aunque el .env diga true).
from pathlib import Path  # noqa: E402

from for3s_core.config import _load_dotenv  # noqa: E402

_load_dotenv(Path.cwd() / ".env")

SESSION_OWNER = os.environ.get("FOR3S_OWNER_SESSION", "brian").strip()
MICROGLIA_CONFIRMAR = os.environ.get("FOR3S_MICROGLIA_CONFIRMAR", "false").strip().lower() == "true"
HORA_BACKUP_UTC = 7  # 01:00 México (ANTES de CLS, red de seguridad)
HORA_CLS_UTC = 8  # 02:00 México
HORA_STATUS_UTC = 8  # 02:30 México (AI4, justo DESPUÉS de CLS — usa minute=30)
HORA_RELEVANCE_UTC = 8  # 02:45 México (recalcula decay ANTES de Microglía)
HORA_MICROGLIA_UTC = 9  # 03:00 México
HORA_CURAR_SKILLS_UTC = 9  # 03:30 México (DESPUÉS de Microglía — usa minute=30)
HORA_DMN_NOCHE_UTC = 10  # 04:00 México (H9 DMN nocturno — DESPUÉS de la curación)
HORA_HEALTH_UTC = 10  # 04:30 México (health check, tras todos los jobs — usa minute=30)


import functools  # noqa: E402
import time as _time  # noqa: E402


def registra_corrida(nombre: str):
    """Decorador para jobs nocturnos: registra cada corrida en cron_corridas
    (job, ok, resultado, ms, creado_at) — PR2.2a. Así /salud nocturno y las alertas
    saben CUÁNDO corrió cada job y con qué resultado, no solo los logs efímeros.
    Defensivo: si el registro falla, NO rompe el job (el job es lo importante)."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(ctx: dict) -> str:
            t0 = _time.monotonic()
            ok = True
            try:
                msg = await fn(ctx)
            except Exception as e:  # noqa: BLE001 — relanzar tras registrar
                ok = False
                msg = f"{nombre} EXCEPCIÓN: {type(e).__name__}"
                await _registrar_corrida(nombre, False, msg, int((_time.monotonic() - t0) * 1000))
                raise
            # heurística: si el msg dice "error", marcar ok=False
            if isinstance(msg, str) and "error" in msg.lower():
                ok = False
            await _registrar_corrida(nombre, ok, str(msg)[:500], int((_time.monotonic() - t0) * 1000))
            return msg

        return wrapper

    return deco


async def _registrar_corrida(job: str, ok: bool, resultado: str, ms: int) -> None:
    """Inserta una corrida en cron_corridas. Defensivo (un fallo de registro no
    tumba el worker)."""
    pool = None
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cron_corridas (job, ok, resultado, ms) VALUES ($1, $2, $3, $4)",
                job, ok, resultado, ms,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[cron_corridas] no pude registrar %s: %s", job, type(e).__name__)
    finally:
        if pool is not None:
            await pool.close()


async def _get_pool():
    """Crea un pool de BD para el job (lo cierra el caller)."""
    from for3s_core import db
    from for3s_core.config import load_settings

    s = load_settings()
    return await db.connect(s.database_url)


async def _sesiones_vivas(pool) -> list[str]:
    """BUG-18 (2026-06-30): TODAS las sesiones con memoria viva embebida — del DUEÑO,
    de sus TEMAS (brian:backend...) Y de los MIEMBROS (tg:<uid>). Antes el ciclo
    nocturno (CLS/Microglía) operaba SOLO sobre SESSION_OWNER='brian' → la memoria de
    los miembros y de otros temas NUNCA se consolidaba al grafo ni se podaba. job_cls y
    job_microglia ahora iteran esto (como job_relevance ya hacía). Las sesiones de test
    sin embedding se saltan solas (filtro embedding IS NOT NULL)."""
    rows = await pool.fetch(
        "SELECT DISTINCT session_id FROM episodes_events "
        "WHERE deleted_at IS NULL AND embedding IS NOT NULL"
    )
    return [r["session_id"] for r in rows]


# --- Jobs ------------------------------------------------------------------
async def ping(ctx: dict) -> str:
    """Job trivial de prueba (Sub-paso 1). Verifica que el worker ejecuta jobs."""
    momento = ahora_local().isoformat()
    msg = f"pong @ {momento} (job_id={ctx.get('job_id', '?')})"
    logger.info("[ping] %s", msg)
    return msg


@registra_corrida("cls")
async def job_cls(ctx: dict) -> str:
    """Job nocturno CLS (2 AM México): consolida episodios pendientes → conceptos
    al Knowledge Graph. BUG-18 (2026-06-30): ahora procesa TODAS las sesiones (dueño +
    temas + miembros), no solo 'brian' — la memoria de los miembros también madura.
    Defensivo: una sesión que falle no frena las demás ni tumba el worker."""
    from for3s_core import consolidator

    pool = None
    try:
        pool = await _get_pool()
        sesiones = await _sesiones_vivas(pool)
        tot_clusters = tot_conceptos = tot_marcados = 0
        n_ses = 0
        for sid in sesiones:
            try:
                r = await consolidator.consolidar(pool, sid, dry_run=False)
                tot_clusters += r.clusters
                tot_conceptos += r.conceptos_escritos
                tot_marcados += r.episodios_marcados
                n_ses += 1
            except Exception as e:  # noqa: BLE001 — una sesión mala no frena al resto
                logger.warning("[job_cls] sesión %s falló: %s", sid, type(e).__name__)
        msg = (
            f"CLS: {n_ses} sesiones · clusters={tot_clusters} "
            f"conceptos={tot_conceptos} marcados={tot_marcados}"
        )
        logger.info("[job_cls] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001 — un fallo nocturno no debe tumbar el worker
        logger.exception("[job_cls] falló: %s", type(e).__name__)
        return f"CLS error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


@registra_corrida("status")
async def job_status(ctx: dict) -> str:
    """Job nocturno STATUS (2:30 AM México, DESPUÉS de CLS): regenera el STATUS
    curado de cada hilo activo (AI4 auto-retomar). Defensivo: un STATUS fallido no
    tumba el worker (generar_status ya traga sus errores por hilo). Espacia las
    llamadas LLM para no topar el rate-limit (anti-429, como CLS)."""
    from for3s_core import hilo_status

    pool = None
    try:
        pool = await _get_pool()
        activos = await hilo_status.hilos_activos(pool, dias=7)
        hechos = 0
        for sid in activos:
            r = await hilo_status.generar_status(pool, sid)
            if r:
                hechos += 1
            await asyncio.sleep(3)  # espaciar (anti rate-limit OAuth, como CLS)
        msg = f"STATUS: {hechos}/{len(activos)} hilos resumidos"
        logger.info("[job_status] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001 — un fallo nocturno no tumba el worker
        logger.exception("[job_status] falló: %s", type(e).__name__)
        return f"STATUS error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


@registra_corrida("relevance")
async def job_relevance(ctx: dict) -> str:
    """Job nocturno RELEVANCE (2:45 AM México, ANTES de Microglía): recalcula la
    columna `relevance` (decay por desuso + refuerzo por uso) de TODAS las sesiones
    con memoria embebida — no solo el dueño. Sin esto, relevance queda congelada/NULL
    y la Microglía nunca encuentra candidatos (BUG-1). Defensivo: un fallo no tumba
    el worker; cada sesión en su try para que una mala no frene a las demás."""
    from for3s_core import relevance

    pool = None
    try:
        pool = await _get_pool()
        # sesiones con al menos un turno vivo y embebido (las de test sin embedding
        # se saltan solas). Recalcular por sesión (la función opera por session_id).
        rows = await pool.fetch(
            "SELECT DISTINCT session_id FROM episodes_events "
            "WHERE deleted_at IS NULL AND embedding IS NOT NULL"
        )
        total_sesiones = 0
        total_filas = 0
        for r in rows:
            sid = r["session_id"]
            try:
                n = await relevance.recalcular_relevance_lote(pool, sid)
                total_filas += n
                total_sesiones += 1
            except Exception as e:  # noqa: BLE001 — una sesión mala no frena las demás
                logger.warning("[job_relevance] sesión %s falló: %s", sid, type(e).__name__)
        msg = f"relevance recalculada: {total_sesiones} sesiones, {total_filas} turnos"
        logger.info("[job_relevance] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001 — un fallo nocturno no tumba el worker
        logger.exception("[job_relevance] falló: %s", type(e).__name__)
        return f"relevance error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


@registra_corrida("microglia")
async def job_microglia(ctx: dict) -> str:
    """Job nocturno Microglía (3 AM México, DESPUÉS de CLS): evalúa el olvido.
    Por defecto DRY-RUN (solo reporta). Borra de verdad solo si MICROGLIA_CONFIRMAR.
    Defensivo."""
    from for3s_core import microglia

    pool = None
    try:
        pool = await _get_pool()
        sesiones = await _sesiones_vivas(pool)
        modo = "REAL" if MICROGLIA_CONFIRMAR else "DRY-RUN"
        tot_cand = tot_olv = 0
        n_ses = 0
        for sid in sesiones:
            try:
                r = await microglia.olvidar(pool, sid, confirmar=MICROGLIA_CONFIRMAR)
                tot_cand += r.candidatos
                tot_olv += r.olvidados
                n_ses += 1
            except Exception as e:  # noqa: BLE001 — una sesión mala no frena al resto
                logger.warning("[job_microglia] sesión %s falló: %s", sid, type(e).__name__)
        msg = f"Microglía [{modo}]: {n_ses} sesiones · candidatos={tot_cand} olvidados={tot_olv}"
        logger.info("[job_microglia] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001
        logger.exception("[job_microglia] falló: %s", type(e).__name__)
        return f"Microglía error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


@registra_corrida("backup")
async def job_backup(ctx: dict) -> str:
    """Job nocturno de backup (1 AM México, ANTES de CLS): pg_dump verificado +
    rotación de los viejos. Es la red de seguridad antes del olvido. Defensivo."""
    from for3s_core import backup
    from for3s_core.config import load_settings

    try:
        s = load_settings()
        ruta, borrados = await asyncio.to_thread(backup.backup_y_rotar, s.database_url)
        msg = f"backup OK: {ruta.name} (rotados {borrados} viejos)"
        logger.info("[job_backup] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001 — un fallo de backup no debe tumbar el worker
        logger.exception("[job_backup] falló: %s", type(e).__name__)
        return f"backup error: {type(e).__name__}"


@registra_corrida("curar_skills")
async def job_curar_skills(ctx: dict) -> str:
    """Job nocturno de curación de skills (H12 P3, 3:30 AM México, DESPUÉS de
    Microglía): las skills AUTO sin uso se degradan poco a poco (active→stale→
    archived), recuperable. Reusa la filosofía de H6 (degradar, no borrar). NUNCA
    toca skills del usuario ni pinned. Defensivo."""
    from for3s_core.aprende import curar_skills

    pool = None
    try:
        pool = await _get_pool()
        r = await curar_skills(pool, confirmar=True)
        logger.info("[job_curar_skills] %s", r)
        return str(r)
    except Exception as e:  # noqa: BLE001 — no debe tumbar el worker
        logger.exception("[job_curar_skills] falló: %s", type(e).__name__)
        return f"curar_skills error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


@registra_corrida("dmn_noche")
async def job_dmn_noche(ctx: dict) -> str:
    """DMN nocturno (H9, 04:00 México, DESPUÉS de la curación): corre TODAS las tasks
    del DMN (incluidas las pesadas, solo_noche=True) — idle garantizado de madrugada.
    Las generativas solo corren si están encendidas (default OFF). Defensivo."""
    from for3s_core import (
        dmn,
        dmn_tasks,  # noqa: F401 — registra las housekeeping al importar
    )

    pool = None
    try:
        pool = await _get_pool()
        rep = await dmn.correr_ciclo(pool, solo_noche=True, forzar=True)
        logger.info("[job_dmn_noche] %s", rep)
        return str(rep)
    except Exception as e:  # noqa: BLE001 — no debe tumbar el worker
        logger.exception("[job_dmn_noche] falló: %s", type(e).__name__)
        return f"dmn_noche error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


@registra_corrida("dmn_idle")
async def job_dmn_idle(ctx: dict) -> str:
    """DMN oportunista de DÍA (H9, cada 30 min): si el sistema está idle (≥N min sin
    uso), corre solo las tasks LIGERAS (solo_noche=False) — aprovecha los ratos
    muertos sin molestar. Si no está idle, no hace nada. Defensivo.

    PR2.3 (2026-06-30): antes era el ÚNICO job SIN @registra_corrida → sus corridas
    eran invisibles en cron_corridas (y en /salud nocturno y Grafana). Ahora se
    registra como los otros 8, para observabilidad completa del scheduler."""
    from for3s_core import (
        dmn,
        dmn_tasks,  # noqa: F401 — registra las housekeeping al importar
    )

    pool = None
    try:
        pool = await _get_pool()
        rep = await dmn.correr_ciclo(pool, solo_noche=False, forzar=False)
        logger.info("[job_dmn_idle] %s", rep)
        return str(rep)
    except Exception as e:  # noqa: BLE001
        logger.exception("[job_dmn_idle] falló: %s", type(e).__name__)
        return f"dmn_idle error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


# --- Worker settings (Arq lee esta clase) ----------------------------------
async def _alertar_dueno(texto: str) -> bool:
    """Envía una alerta al dueño por Telegram (worker → API Telegram). Lee el
    owner_id del telegram_owner.json (montado) + el bot token (cifrado, KEK).
    Defensivo: si algo falta, NO rompe (devuelve False). PR2.2b."""
    import json as _json

    from for3s_core import db as _db
    from for3s_core.config import load_settings as _ls
    from for3s_core.secret_store import SecretStore as _SS

    # owner_id del json (cwd=/app → /app/.for3s, montado)
    owner = None
    for cand in (Path.cwd() / ".for3s" / "telegram_owner.json",
                 Path("/app/.for3s/telegram_owner.json"),
                 Path("/root/.for3s/telegram_owner.json")):
        try:
            if cand.exists():
                owner = _json.loads(cand.read_text()).get("owner_id")
                break
        except Exception:  # noqa: BLE001
            continue
    if owner is None:
        logger.warning("[alerta] no encuentro owner_id → no alerto")
        return False

    pool = None
    try:
        s = _ls()
        pool = await _get_pool()
        tok = await _SS(pool).get_secret(s.owner_session, "telegram_bot_token")
        if not tok:
            logger.warning("[alerta] sin bot token → no alerto")
            return False
        import httpx

        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": owner, "text": texto},
            )
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001 — una alerta fallida no tumba el worker
        logger.warning("[alerta] fallo el envio: %s", type(e).__name__)
        return False
    finally:
        if pool is not None:
            await pool.close()


@registra_corrida("health_check")
async def job_health_check(ctx: dict) -> str:
    """Job nocturno (4:30 AM Mx, tras todos los demás): corre el reporte de salud
    completo y, SI hay 🔴 FALLAS (no avisos), ALERTA al dueño por Telegram. PR2.2b.
    Es lo que hace que un subsistema roto NO pase desapercibido (la lección de los 9
    bugs de la contenerización). Defensivo: un fallo aquí no tumba el worker."""
    from for3s_core import health

    pool = None
    try:
        pool = await _get_pool()
        reporte = await health.reporte_completo(pool)
        n_fail = reporte.count("🔴")
        if n_fail > 0:
            # extraer SOLO las líneas con 🔴 para una alerta concisa
            fallas = [ln.strip() for ln in reporte.split("\n") if "🔴" in ln]
            alerta = (
                f"🚨 *For3s OS: {n_fail} problema(s) de salud detectados*\n\n"
                + "\n".join(fallas[:15])
                + "\n\nUsa /salud para el reporte completo."
            )
            enviado = await _alertar_dueno(alerta)
            msg = f"health_check: {n_fail} fallas → alerta {'enviada' if enviado else 'NO enviada'}"
        else:
            msg = "health_check: todo OK (sin alerta)"
        logger.info("[job_health_check] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001
        logger.exception("[job_health_check] falló: %s", type(e).__name__)
        return f"health_check error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


class WorkerSettings:
    """Configuración que Arq usa para levantar el worker.

    Uso: `arq for3s_core.tasks.WorkerSettings`
    """

    redis_settings = REDIS_SETTINGS
    functions = [
        ping,
        job_cls,
        job_status,
        job_relevance,
        job_microglia,
        job_backup,
        job_curar_skills,
        job_dmn_noche,
        job_health_check,
        job_dmn_idle,
    ]
    cron_jobs = [
        cron(job_backup, hour=HORA_BACKUP_UTC, minute=0),  # 01:00 México (1º)
        cron(job_cls, hour=HORA_CLS_UTC, minute=0),  # 02:00 México
        cron(job_status, hour=HORA_STATUS_UTC, minute=30),  # 02:30 México (AI4)
        cron(job_relevance, hour=HORA_RELEVANCE_UTC, minute=45),  # 02:45 México (decay, BUG-1)
        cron(job_microglia, hour=HORA_MICROGLIA_UTC, minute=0),  # 03:00 México
        cron(job_curar_skills, hour=HORA_CURAR_SKILLS_UTC, minute=30),  # 03:30 México (H12 P3)
        cron(job_dmn_noche, hour=HORA_DMN_NOCHE_UTC, minute=0),  # 04:00 México (H9, todas)
        cron(job_health_check, hour=HORA_HEALTH_UTC, minute=30),  # 04:30 México (PR2.2b alerta)
        cron(job_dmn_idle, minute={0, 30}),  # H9: cada 30 min, corre solo si está idle (ligeras)
    ]

    @staticmethod
    async def on_startup(ctx: dict) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        logger.info(
            "[worker] arrancado — Valkey db=%s · CLS %02d:00 UTC · Microglía %02d:00 UTC "
            "(confirmar=%s) · owner=%s",
            VALKEY_DB_SCHEDULER,
            HORA_CLS_UTC,
            HORA_MICROGLIA_UTC,
            MICROGLIA_CONFIRMAR,
            SESSION_OWNER,
        )

    @staticmethod
    async def on_shutdown(ctx: dict) -> None:
        logger.info("[worker] apagado")
