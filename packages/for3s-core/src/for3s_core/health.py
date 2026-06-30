# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — SALUD / MONITOREO end-to-end (PR2, 2026-06-29).

El problema que resuelve (lección de los 8 bugs de la contenerización): varios
subsistemas se rompieron EN SILENCIO y nadie se enteró (backup, decay, CLS, GitHub,
render). Este módulo VIGILA toda la línea — desde que llega un mensaje hasta que toca
la memoria — + tokens por persona + integraciones + ciclo nocturno + subsistemas.

Diseño (PR2.1a — opción "infiere por efectos"): NO crea tablas nuevas ni toca el cron.
Lee el estado REAL de lo que ya existe (BD, audit, episodes, grafo, backups en disco,
hermanos por HTTP) e infiere la salud. Cada check devuelve un estado: OK / WARN / FAIL.

Cada función es independiente y DEFENSIVA: si un check falla, devuelve FAIL con el
motivo, nunca tumba el reporte completo. Reutilizable por el comando /salud y, más
adelante, por las alertas nocturnas (PR2.2).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import asyncpg

OK = "✅"
WARN = "⚠️"
FAIL = "🔴"

# Carpeta de backups (misma que backup.py — donde caen los .sql automáticos).
BACKUP_DIR = os.environ.get("FOR3S_BACKUP_DIR", os.path.expanduser("~/for3s-backups"))
BACKUP_PREFIJO = "auto_for3s_"


@dataclass
class Check:
    """Un chequeo de salud: nombre, estado (OK/WARN/FAIL) y detalle legible."""

    nombre: str
    estado: str
    detalle: str


# ===========================================================================
# 1. LA LÍNEA end-to-end (mensaje → identidad → Claude → memoria)
# ===========================================================================
async def salud_linea(pool: asyncpg.Pool) -> list[Check]:
    """Vigila la cadena completa de un turno, por sus rastros en audit/episodes.

    message_in → secret_read (KEK) → confidence → message_out → episodes_events.
    Mide el GAP in/out RECIENTE (24h): si entran mensajes pero no salen respuestas,
    algo se está tragando errores. (El gap histórico viejo era de comandos/529, hoy 0.)
    """
    checks: list[Check] = []
    async with pool.acquire() as conn:
        # ¿la línea está VIVA? último turno hace cuánto
        ult = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (now() - max(created_at)))/3600 FROM episodes_events"
        )
        if ult is None:
            checks.append(Check("Línea viva", FAIL, "no hay turnos en la memoria"))
        else:
            h = float(ult)
            est = OK if h < 48 else WARN
            checks.append(Check("Línea viva", est, f"último turno hace {h:.1f}h"))

        # GAP in/out RECIENTE (24h): entran vs salen
        in24 = await conn.fetchval(
            "SELECT count(*) FROM audit_events WHERE action='message_in' AND ts > now()-interval '24 hours'"
        )
        out24 = await conn.fetchval(
            "SELECT count(*) FROM audit_events WHERE action='message_out' AND ts > now()-interval '24 hours'"
        )
        gap = int(in24) - int(out24)
        # gap pequeño es normal (comandos no generan message_out). gap grande = alerta.
        est = OK if gap <= 3 else (WARN if gap <= 10 else FAIL)
        checks.append(
            Check("Flujo in→out (24h)", est, f"{in24} entraron, {out24} respondidas (gap {gap})")
        )

        # ¿la KEK descifra? (secret_read reciente sin fallos)
        sr = await conn.fetchval(
            "SELECT count(*) FROM audit_events WHERE action='secret_read' AND ts > now()-interval '24 hours'"
        )
        checks.append(Check("KEK / secrets", OK, f"{sr} lecturas de secreto (24h)"))

        # ¿confidence (metacognición) corre?
        cf = await conn.fetchval(
            "SELECT count(*) FROM audit_events WHERE action='confidence_calculated'"
        )
        est = OK if int(cf) > 0 else WARN
        checks.append(Check("Metacognición", est, f"{cf} evaluaciones de confianza"))

        # USO REAL de GitHub: la señal correcta es detail.tools del message_out (NO el
        # viejo 'gh_fetched' que ya nadie escribe). Mide cuándo se usaron tools por última vez.
        gh = await conn.fetchval(
            "SELECT max(ts) FROM audit_events WHERE action='message_out' "
            "AND jsonb_array_length(COALESCE(detail->'tools','[]'::jsonb)) > 0"
        )
        if gh is None:
            checks.append(Check("Uso de tools (GitHub)", WARN, "sin registro de uso de tools"))
        else:
            hgh = await conn.fetchval("SELECT EXTRACT(EPOCH FROM (now()-$1))/3600", gh)
            checks.append(Check("Uso de tools (GitHub)", OK, f"último uso hace {float(hgh):.0f}h"))
    return checks


# ===========================================================================
# 2. TOKENS por persona + global
# ===========================================================================
async def salud_tokens(pool: asyncpg.Pool) -> list[Check]:
    """Consumo de tokens por persona (telegram_user_id) + global acumulado.
    Honesto: avisa de los turnos LEGADO sin autor (no infla ni miente)."""
    checks: list[Check] = []
    async with pool.acquire() as conn:
        tot_in = await conn.fetchval("SELECT COALESCE(sum(tokens_in),0) FROM episodes_events") or 0
        tot_out = await conn.fetchval("SELECT COALESCE(sum(tokens_out),0) FROM episodes_events") or 0
        checks.append(
            Check("Tokens GLOBAL", OK, f"{int(tot_in):,} in · {int(tot_out):,} out")
        )
        # por persona (solo los que tienen autor)
        filas = await conn.fetch(
            "SELECT telegram_user_id AS uid, sum(tokens_in) ti, sum(tokens_out) to_ "
            "FROM episodes_events WHERE telegram_user_id IS NOT NULL "
            "GROUP BY telegram_user_id ORDER BY sum(tokens_in) DESC"
        )
        for f in filas:
            checks.append(
                Check(
                    f"Tokens user {f['uid']}",
                    OK,
                    f"{int(f['ti'] or 0):,} in · {int(f['to_'] or 0):,} out",
                )
            )
        # legado sin atribuir (honestidad)
        leg = await conn.fetchval(
            "SELECT count(*) FROM episodes_events WHERE telegram_user_id IS NULL"
        )
        if int(leg) > 0:
            checks.append(
                Check("Tokens legado", WARN, f"{leg} turnos sin autor (previos al registro)")
            )
        # turnos assistant con tokens=0 (no se midió su consumo — honestidad)
        tk0 = await conn.fetchval(
            "SELECT count(*) FROM episodes_events WHERE role='assistant' AND tokens_in=0"
        )
        if int(tk0) > 0:
            checks.append(
                Check("Tokens sin medir", WARN, f"{tk0} respuestas con tokens=0 (no contabilizadas)")
            )
    return checks


# ===========================================================================
# 3. HILOS (sessions, aislamiento por persona×tema)
# ===========================================================================
async def salud_hilos(pool: asyncpg.Pool) -> list[Check]:
    """Estado de los hilos: cuántos vivos, cuáles reales vs test, última actividad."""
    checks: list[Check] = []
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT count(DISTINCT session_id) FROM episodes_events WHERE deleted_at IS NULL"
        )
        reales = await conn.fetch(
            "SELECT session_id, count(*) n, max(created_at) ult FROM episodes_events "
            "WHERE deleted_at IS NULL AND session_id NOT LIKE 'test-%' AND session_id <> 'diag_repro' "
            "GROUP BY session_id ORDER BY n DESC"
        )
        checks.append(Check("Hilos totales", OK, f"{total} sesiones ({len(reales)} reales)"))
        for r in reales:
            horas = await conn.fetchval(
                "SELECT EXTRACT(EPOCH FROM (now()-$1))/3600", r["ult"]
            )
            checks.append(
                Check(f"Hilo {r['session_id']}", OK, f"{r['n']} turnos · activo hace {float(horas):.0f}h")
            )
    return checks


# ===========================================================================
# 4. SUBSISTEMAS (los 8 bugs: BD, backup, decay, grafo, embeddings, audit)
# ===========================================================================
async def salud_subsistemas(pool: asyncpg.Pool) -> list[Check]:
    """Chequea los subsistemas que se rompieron en silencio (lección de los bugs)."""
    checks: list[Check] = []
    async with pool.acquire() as conn:
        # BD viva
        try:
            await conn.fetchval("SELECT 1")
            checks.append(Check("BD Postgres", OK, "responde"))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("BD Postgres", FAIL, f"no responde: {type(e).__name__}"))

        # BACKUP reciente (BUG-5): hay un .sql de las últimas 48h?
        try:
            recientes = []
            if os.path.isdir(BACKUP_DIR):
                ahora = time.time()
                for f in os.listdir(BACKUP_DIR):
                    if f.startswith(BACKUP_PREFIJO) and f.endswith(".sql"):
                        edad_h = (ahora - os.path.getmtime(os.path.join(BACKUP_DIR, f))) / 3600
                        recientes.append(edad_h)
            if not recientes:
                checks.append(Check("Backup", FAIL, "NO hay backups en disco"))
            else:
                mas_nuevo = min(recientes)
                est = OK if mas_nuevo < 48 else WARN
                checks.append(
                    Check("Backup", est, f"último hace {mas_nuevo:.0f}h · {len(recientes)} en disco")
                )
        except Exception as e:  # noqa: BLE001
            checks.append(Check("Backup", FAIL, f"error: {type(e).__name__}"))

        # DECAY (BUG-1): turnos vivos con embedding pero SIN relevance = decay no corre
        sin_rel = await conn.fetchval(
            "SELECT count(*) FROM episodes_events "
            "WHERE deleted_at IS NULL AND embedding IS NOT NULL AND relevance IS NULL"
        )
        est = OK if int(sin_rel) == 0 else WARN
        checks.append(Check("Decay (relevance)", est, f"{sin_rel} turnos sin relevance"))

        # MEMORIA semántica: turnos vivos sin embedding
        sin_emb = await conn.fetchval(
            "SELECT count(*) FROM episodes_events WHERE deleted_at IS NULL AND embedding IS NULL"
        )
        est = OK if int(sin_emb) == 0 else WARN
        checks.append(Check("Embeddings", est, f"{sin_emb} turnos sin vectorizar"))

        # AUDIT chain íntegra
        try:
            from for3s_core import audit

            ok, n = await audit.verify_chain(pool)
            checks.append(
                Check("Audit chain", OK if ok else FAIL, f"{n} entradas {'íntegra' if ok else 'ROTA'}")
            )
        except Exception as e:  # noqa: BLE001
            checks.append(Check("Audit chain", WARN, f"no verificable: {type(e).__name__}"))
    return checks


# ===========================================================================
# 5. GRAFO de conocimiento (BUG-8: CLS escribe al grafo AGE)
# ===========================================================================
async def salud_grafo(pool: asyncpg.Pool) -> list[Check]:
    """¿El grafo crece? (conceptos/episodios) + ¿el catálogo AGE está sano? (BUG-8)."""
    checks: list[Check] = []
    try:
        from for3s_core import kg

        st = await kg.stats(pool)
        n = st.get("conceptos", 0) if isinstance(st, dict) else 0
        checks.append(Check("Grafo (KG)", OK, f"conceptos: {st}"))
    except Exception as e:  # noqa: BLE001
        checks.append(Check("Grafo (KG)", FAIL, f"no legible: {type(e).__name__}: {e}"))
    # pendientes de consolidar
    async with pool.acquire() as conn:
        pend = await conn.fetchval(
            "SELECT count(*) FROM episodes_events "
            "WHERE deleted_at IS NULL AND embedding IS NOT NULL AND consolidated_to_kg = false"
        )
        checks.append(Check("CLS pendientes", OK, f"{pend} turnos por consolidar"))
    return checks


# ===========================================================================
# 6. INTEGRACIONES (hermanos de red: GitHub MCP, render) + Valkey
# ===========================================================================
async def salud_integraciones() -> list[Check]:
    """Chequea los hermanos de red por HTTP (GitHub MCP, render) — defensivo."""
    import httpx

    checks: list[Check] = []
    objetivos = [
        ("GitHub MCP", os.environ.get("FOR3S_GITHUB_MCP_URL", "http://github-mcp:8082/mcp")),
        ("GitHub MCP write", os.environ.get("FOR3S_GITHUB_MCP_WRITE_URL", "http://github-mcp-write:8082/mcp")),
        ("Render", os.environ.get("FOR3S_RENDER_URL", "http://render:8080/").rstrip("/") + "/health"),
    ]
    for nombre, url in objetivos:
        try:
            async with httpx.AsyncClient(timeout=5) as cli:
                r = await cli.get(url)
            # 200/401/406 = el server está VIVO (401 = pide auth, normal sin token)
            vivo = r.status_code in (200, 401, 406, 400)
            checks.append(
                Check(nombre, OK if vivo else WARN, f"HTTP {r.status_code}")
            )
        except Exception as e:  # noqa: BLE001
            checks.append(Check(nombre, FAIL, f"no responde: {type(e).__name__}"))
    return checks


# ===========================================================================
# 7. CICLO NOCTURNO (infiere por EFECTOS: backup, grafo, decay, dmn)
# ===========================================================================
async def salud_nocturno(pool: asyncpg.Pool) -> list[Check]:
    """Salud del ciclo nocturno inferida por efectos (PR2.1a, sin tabla nueva).
    backup→¿reciente? · CLS→¿consolidó? · decay→¿recalculó hoy? · DMN→dmn_corridas."""
    checks: list[Check] = []
    async with pool.acquire() as conn:
        # PR2.2a: leer cron_corridas (la tabla con TIMESTAMP real). Por cada job
        # nocturno: ¿cuándo fue su última corrida y con qué resultado?
        jobs = ["backup", "cls", "relevance", "microglia", "status", "curar_skills", "dmn_noche"]
        for job in jobs:
            row = await conn.fetchrow(
                "SELECT ok, resultado, creado_at, "
                "EXTRACT(EPOCH FROM (now()-creado_at))/3600 AS horas "
                "FROM cron_corridas WHERE job=$1 ORDER BY creado_at DESC LIMIT 1",
                job,
            )
            if row is None:
                checks.append(Check(f"Job {job}", WARN, "sin corridas registradas aún"))
            else:
                h = float(row["horas"])
                # un job nocturno debería haber corrido en las últimas ~26h
                est = (FAIL if not row["ok"] else (OK if h < 26 else WARN))
                checks.append(
                    Check(f"Job {job}", est, f"hace {h:.0f}h · {row['resultado'][:45]}")
                )
        # decay corrió: ¿la mayoría de turnos vivos tienen relevance?
        con_rel = await conn.fetchval(
            "SELECT count(*) FROM episodes_events WHERE deleted_at IS NULL AND relevance IS NOT NULL"
        )
        tot = await conn.fetchval("SELECT count(*) FROM episodes_events WHERE deleted_at IS NULL")
        checks.append(Check("Decay aplicado", OK, f"{con_rel}/{tot} turnos con relevance"))
    return checks


# ===========================================================================
# REPORTE COMPLETO
# ===========================================================================
_SECCIONES = {
    "linea": ("🔗 LÍNEA (mensaje→memoria)", salud_linea),
    "subsistemas": ("🧠 SUBSISTEMAS", salud_subsistemas),
    "grafo": ("📊 GRAFO", salud_grafo),
    "integraciones": ("🔌 INTEGRACIONES", None),  # no recibe pool
    "nocturno": ("🌙 NOCTURNO", salud_nocturno),
    "tokens": ("💰 TOKENS", salud_tokens),
    "hilos": ("🧵 HILOS", salud_hilos),
}


async def reporte_seccion(pool: asyncpg.Pool, seccion: str) -> str:
    """Reporte de UNA sección detallada (/salud <seccion>). Para no saturar el chat
    cuando solo quieres ver tokens, o la línea, o el nocturno, etc."""
    s = seccion.strip().lower()
    if s not in _SECCIONES:
        opciones = ", ".join(_SECCIONES.keys())
        return f"🩺 Sección desconocida. Usa: /salud (todo) o /salud <{opciones}>"
    titulo, fn = _SECCIONES[s]
    try:
        cs = await (salud_integraciones() if fn is None else fn(pool))
    except Exception as e:  # noqa: BLE001
        return f"🩺 *{titulo}*\n{FAIL} error: {type(e).__name__}: {e}"
    lineas = [f"🩺 *{titulo}*"]
    for c in cs:
        lineas.append(f"{c.estado} {c.nombre}: {c.detalle}")
    return "\n".join(lineas)


async def reporte_completo(pool: asyncpg.Pool) -> str:
    """Junta todos los checks en un reporte Markdown para Telegram. Defensivo:
    cada sección en su try para que una falla no rompa el reporte entero."""
    secciones: list[tuple[str, list[Check]]] = []

    async def _safe(nombre, coro):
        try:
            secciones.append((nombre, await coro))
        except Exception as e:  # noqa: BLE001
            secciones.append((nombre, [Check(nombre, FAIL, f"error: {type(e).__name__}: {e}")]))

    await _safe("🔗 LÍNEA (mensaje→memoria)", salud_linea(pool))
    await _safe("🧠 SUBSISTEMAS", salud_subsistemas(pool))
    await _safe("📊 GRAFO", salud_grafo(pool))
    await _safe("🔌 INTEGRACIONES", salud_integraciones())
    await _safe("🌙 NOCTURNO", salud_nocturno(pool))
    await _safe("💰 TOKENS", salud_tokens(pool))
    await _safe("🧵 HILOS", salud_hilos(pool))

    # resumen: cuántos FAIL/WARN
    todos = [c for _, cs in secciones for c in cs]
    n_fail = sum(1 for c in todos if c.estado == FAIL)
    n_warn = sum(1 for c in todos if c.estado == WARN)
    cabecera = "🩺 *SALUD DE FOR3S OS*\n"
    if n_fail:
        cabecera += f"{FAIL} {n_fail} problemas · "
    if n_warn:
        cabecera += f"{WARN} {n_warn} avisos · "
    if not n_fail and not n_warn:
        cabecera += f"{OK} todo en orden\n"
    else:
        cabecera += "revisa abajo\n"

    lineas = [cabecera]
    for nombre, cs in secciones:
        lineas.append(f"\n*{nombre}*")
        for c in cs:
            lineas.append(f"{c.estado} {c.nombre}: {c.detalle}")
    return "\n".join(lineas)
