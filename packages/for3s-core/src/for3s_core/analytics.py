# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — ANALÍTICA / DATOS de uso (PR3, 2026-06-30).

Responde "no sabemos NADA del uso": actividad por día, consumo de tokens, temas
(repos) recurrentes, capacidades usadas, actividad por persona.

⚠️ DISEÑO HONESTO (auditoría profunda PR3.1): cada métrica está verificada contra el
dato REAL para no engañar:
  · ACTIVIDAD/tokens: SOLO turnos vivos (deleted_at IS NULL) — la basura soft-deleted
    (tests, huérfanos) NO cuenta.
  · REPOS: gh_resources tiene 1 fila POR ARCHIVO (kind='file'), no por consulta → contar
    "98 veces" sería FALSO (inflado x5). Se cuenta por SESIONES distintas (consultas reales).
  · TOKENS: ~7% de respuestas tienen tokens=0 (no se midieron) → se AVISA, no se finge exacto.
  · POR PERSONA: hay turnos legado sin telegram_user_id → se AVISA del legado, no se omite.
  · se filtran datos sucios (repo vacío '/').

Distinto de /salud tokens (estado actual): esto es TENDENCIAS y uso. Comando /datos.
"""

from __future__ import annotations

import asyncpg


async def datos_actividad(pool: asyncpg.Pool) -> list[str]:
    """Actividad (turnos) por día, últimos 7 días + total. SOLO turnos vivos."""
    lineas = ["📈 *Actividad (turnos/día, 7d)*"]
    async with pool.acquire() as conn:
        filas = await conn.fetch(
            "SELECT created_at::date AS dia, count(*) n "
            "FROM episodes_events WHERE deleted_at IS NULL "
            "AND created_at > now() - interval '7 days' "
            "GROUP BY created_at::date ORDER BY dia DESC"
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM episodes_events WHERE deleted_at IS NULL"
        )
    for f in filas:
        lineas.append(f"  {f['dia']}: {f['n']} turnos")
    if not filas:
        lineas.append("  (sin actividad en 7 días)")
    lineas.append(f"  TOTAL histórico: {total} turnos vivos")
    return lineas


async def datos_consumo(pool: asyncpg.Pool) -> list[str]:
    """Consumo de tokens por día (7d) + total + aviso de % sin medir (honesto)."""
    lineas = ["💰 *Consumo de tokens (7d)*"]
    async with pool.acquire() as conn:
        filas = await conn.fetch(
            "SELECT created_at::date AS dia, sum(tokens_in) ti, sum(tokens_out) to_ "
            "FROM episodes_events WHERE deleted_at IS NULL "
            "AND created_at > now() - interval '7 days' "
            "GROUP BY created_at::date ORDER BY dia DESC"
        )
        tot = await conn.fetchrow(
            "SELECT COALESCE(sum(tokens_in),0) ti, COALESCE(sum(tokens_out),0) to_ "
            "FROM episodes_events WHERE deleted_at IS NULL"
        )
        # honestidad: % de respuestas sin medir (tokens=0)
        sin = await conn.fetchval(
            "SELECT count(*) FROM episodes_events WHERE deleted_at IS NULL "
            "AND role='assistant' AND tokens_in=0"
        )
        tot_resp = await conn.fetchval(
            "SELECT count(*) FROM episodes_events WHERE deleted_at IS NULL AND role='assistant'"
        )
    for f in filas:
        lineas.append(f"  {f['dia']}: {int(f['ti'] or 0):,} in · {int(f['to_'] or 0):,} out")
    lineas.append(f"  TOTAL: {int(tot['ti']):,} in · {int(tot['to_']):,} out")
    if tot_resp and sin:
        pct = 100.0 * sin / tot_resp
        lineas.append(f"  ⚠️ aprox: {sin} respuestas ({pct:.0f}%) sin medir tokens")
    return lineas


async def datos_repos(pool: asyncpg.Pool) -> list[str]:
    """Repos más consultados — contados por SESIONES distintas (consultas reales),
    NO por filas (gh_resources tiene 1 fila por archivo → inflaría x5). Honesto."""
    lineas = ["🔥 *Repos más consultados*"]
    async with pool.acquire() as conn:
        filas = await conn.fetch(
            "SELECT owner||'/'||repo AS repo, count(DISTINCT session_id) AS veces "
            "FROM gh_resources WHERE owner <> '' AND repo <> '' "  # filtra basura ('/' vacío)
            "GROUP BY owner, repo ORDER BY veces DESC, max(fetched_at) DESC LIMIT 6"
        )
    for f in filas:
        lineas.append(f"  {f['repo']}: {f['veces']} consulta(s)")
    if not filas:
        lineas.append("  (sin repos consultados)")
    return lineas


async def datos_capacidades(pool: asyncpg.Pool) -> list[str]:
    """Qué capacidades/acciones se usan más (de audit). Filtra ruido de tests."""
    lineas = ["🛠️ *Capacidades más usadas*"]
    async with pool.acquire() as conn:
        filas = await conn.fetch(
            "SELECT action, count(*) n FROM audit_events "
            "WHERE action NOT LIKE 'test_%' AND action <> 'immutable_test' "
            "GROUP BY action ORDER BY n DESC LIMIT 7"
        )
    _legible = {
        "message_in": "mensajes recibidos",
        "message_out": "respuestas dadas",
        "secret_read": "uso de secretos (KEK)",
        "confidence_calculated": "metacognición",
        "cls_consolidation": "consolidaciones de memoria",
        "github_write": "escrituras en GitHub",
        "gh_fetched": "lecturas de GitHub (legado)",
    }
    for f in filas:
        nombre = _legible.get(f["action"], f["action"])
        lineas.append(f"  {nombre}: {f['n']}")
    return lineas


async def datos_personas(pool: asyncpg.Pool) -> list[str]:
    """Actividad por persona (turnos, días activo). Avisa del legado sin autor."""
    lineas = ["👤 *Actividad por persona*"]
    async with pool.acquire() as conn:
        filas = await conn.fetch(
            "SELECT telegram_user_id uid, count(*) turnos, "
            "count(DISTINCT created_at::date) dias "
            "FROM episodes_events WHERE deleted_at IS NULL AND telegram_user_id IS NOT NULL "
            "GROUP BY telegram_user_id ORDER BY turnos DESC"
        )
        legado = await conn.fetchval(
            "SELECT count(*) FROM episodes_events "
            "WHERE deleted_at IS NULL AND telegram_user_id IS NULL"
        )
    for f in filas:
        lineas.append(f"  user {f['uid']}: {f['turnos']} turnos · {f['dias']} día(s) activo")
    if legado:
        lineas.append(f"  ⚠️ {legado} turnos legado (sin autor, previos al registro)")
    return lineas


async def reporte_datos(pool: asyncpg.Pool) -> str:
    """Reporte completo de analítica para /datos. Defensivo por sección."""
    secciones = [datos_actividad, datos_consumo, datos_repos, datos_capacidades, datos_personas]
    out = ["📊 *DATOS DE FOR3S OS* (uso real, sin inflar)\n"]
    for fn in secciones:
        try:
            out.append("\n".join(await fn(pool)))
        except Exception as e:  # noqa: BLE001 — una sección no rompe el reporte
            out.append(f"(sección {fn.__name__} falló: {type(e).__name__})")
    return "\n\n".join(out)
