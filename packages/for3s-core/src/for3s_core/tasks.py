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
    host=VALKEY_HOST, port=VALKEY_PORT, database=VALKEY_DB_SCHEDULER,
)

# --- Config de los jobs nocturnos ------------------------------------------
# El worker NO hereda el .env por systemd (igual que el bot). Cargarlo aquí, al
# importar, para que las variables FOR3S_* se lean (si no, MICROGLIA_CONFIRMAR
# quedaría siempre en su default 'false' aunque el .env diga true).
from pathlib import Path  # noqa: E402

from for3s_core.config import _load_dotenv  # noqa: E402

_load_dotenv(Path.cwd() / ".env")

SESSION_OWNER = os.environ.get("FOR3S_OWNER_SESSION", "brian").strip()
MICROGLIA_CONFIRMAR = (
    os.environ.get("FOR3S_MICROGLIA_CONFIRMAR", "false").strip().lower() == "true"
)
HORA_BACKUP_UTC = 7     # 01:00 México (ANTES de CLS, red de seguridad)
HORA_CLS_UTC = 8        # 02:00 México
HORA_STATUS_UTC = 8     # 02:30 México (AI4, justo DESPUÉS de CLS — usa minute=30)
HORA_MICROGLIA_UTC = 9  # 03:00 México
HORA_CURAR_SKILLS_UTC = 9  # 03:30 México (DESPUÉS de Microglía — usa minute=30)
HORA_DMN_NOCHE_UTC = 10    # 04:00 México (H9 DMN nocturno — DESPUÉS de la curación)


async def _get_pool():
    """Crea un pool de BD para el job (lo cierra el caller)."""
    from for3s_core import db
    from for3s_core.config import load_settings
    s = load_settings()
    return await db.connect(s.database_url)


# --- Jobs ------------------------------------------------------------------
async def ping(ctx: dict) -> str:
    """Job trivial de prueba (Sub-paso 1). Verifica que el worker ejecuta jobs."""
    momento = ahora_local().isoformat()
    msg = f"pong @ {momento} (job_id={ctx.get('job_id', '?')})"
    logger.info("[ping] %s", msg)
    return msg


async def job_cls(ctx: dict) -> str:
    """Job nocturno CLS (2 AM México): consolida episodios pendientes → conceptos
    al Knowledge Graph. Defensivo: si falla, loguea pero no tumba el worker."""
    from for3s_core import consolidator
    pool = None
    try:
        pool = await _get_pool()
        r = await consolidator.consolidar(pool, SESSION_OWNER, dry_run=False)
        msg = (f"CLS: clusters={r.clusters} conceptos={r.conceptos_escritos} "
               f"marcados={r.episodios_marcados} (pendientes_eval={r.total_pendientes})")
        logger.info("[job_cls] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001 — un fallo nocturno no debe tumbar el worker
        logger.exception("[job_cls] falló: %s", type(e).__name__)
        return f"CLS error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


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


async def job_microglia(ctx: dict) -> str:
    """Job nocturno Microglía (3 AM México, DESPUÉS de CLS): evalúa el olvido.
    Por defecto DRY-RUN (solo reporta). Borra de verdad solo si MICROGLIA_CONFIRMAR.
    Defensivo."""
    from for3s_core import microglia
    pool = None
    try:
        pool = await _get_pool()
        r = await microglia.olvidar(pool, SESSION_OWNER, confirmar=MICROGLIA_CONFIRMAR)
        modo = "REAL" if MICROGLIA_CONFIRMAR else "DRY-RUN"
        msg = f"Microglía [{modo}]: candidatos={r.candidatos} olvidados={r.olvidados}"
        logger.info("[job_microglia] %s", msg)
        return msg
    except Exception as e:  # noqa: BLE001
        logger.exception("[job_microglia] falló: %s", type(e).__name__)
        return f"Microglía error: {type(e).__name__}"
    finally:
        if pool is not None:
            await pool.close()


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


async def job_dmn_idle(ctx: dict) -> str:
    """DMN oportunista de DÍA (H9, cada 30 min): si el sistema está idle (≥N min sin
    uso), corre solo las tasks LIGERAS (solo_noche=False) — aprovecha los ratos
    muertos sin molestar. Si no está idle, no hace nada. Defensivo."""
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
class WorkerSettings:
    """Configuración que Arq usa para levantar el worker.

    Uso: `arq for3s_core.tasks.WorkerSettings`
    """

    redis_settings = REDIS_SETTINGS
    functions = [ping, job_cls, job_status, job_microglia, job_backup, job_curar_skills,
                 job_dmn_noche, job_dmn_idle]
    cron_jobs = [
        cron(job_backup, hour=HORA_BACKUP_UTC, minute=0),        # 01:00 México (1º)
        cron(job_cls, hour=HORA_CLS_UTC, minute=0),             # 02:00 México
        cron(job_status, hour=HORA_STATUS_UTC, minute=30),       # 02:30 México (AI4)
        cron(job_microglia, hour=HORA_MICROGLIA_UTC, minute=0),  # 03:00 México
        cron(job_curar_skills, hour=HORA_CURAR_SKILLS_UTC, minute=30),  # 03:30 México (H12 P3)
        cron(job_dmn_noche, hour=HORA_DMN_NOCHE_UTC, minute=0),   # 04:00 México (H9, todas)
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
            VALKEY_DB_SCHEDULER, HORA_CLS_UTC, HORA_MICROGLIA_UTC,
            MICROGLIA_CONFIRMAR, SESSION_OWNER,
        )

    @staticmethod
    async def on_shutdown(ctx: dict) -> None:
        logger.info("[worker] apagado")
