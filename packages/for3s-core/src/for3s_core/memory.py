# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""Memoria episódica de For3s OS (H2) — Nodo 2 Hipocampo (versión cruda).

Event Sourcing: cada turno (user dice X, For3s responde Y) es un evento
append-only en episodes_events. El historial de una sesión se reconstruye
leyendo esos eventos en orden → For3s "recuerda" entre reinicios.

(Búsqueda semántica, KG, olvido y consolidación llegan en H5/H6.)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import asyncpg

# Retiene refs de las tareas background de last_accessed para que el GC no las
# cancele (asyncio solo guarda weakrefs a las tareas en vuelo). H6 Sub-paso 3.
_BG_TOCAR: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Turn:
    """Un turno de conversación recuperado de la memoria.

    created_at + telegram_user_id (D-1, Bloque 1): para que el agente se oriente
    por TIEMPO y AUTOR (no solo por significado). Opcionales con default None →
    retrocompatibles: todo consumidor que solo use role/content sigue igual."""

    role: str  # "user" | "assistant"
    content: str
    created_at: object = None  # datetime del turno (cuándo se dijo)
    telegram_user_id: int | None = None  # quién lo dijo (autor; None = sistema/legado)


async def ensure_session(pool: asyncpg.Pool, session_id: str, *, channel: str = "cli") -> None:
    """Crea la sesión si no existe (idempotente)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (id, channel) VALUES ($1, $2)
            ON CONFLICT (id) DO NOTHING
            """,
            session_id,
            channel,
        )


async def record_turn(
    pool: asyncpg.Pool,
    session_id: str,
    *,
    role: str,
    content: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str | None = None,
    channel: str = "cli",
    owner_user_id: int | None = None,
    equipo_id: int | None = None,
    telegram_user_id: int | None = None,
) -> int:
    """Guarda un turno como evento append-only. Devuelve su seq.

    channel: por qué puerta entró este turno ('cli' | 'telegram'). Se guarda
    POR TURNO (no por sesión) — CLI y Telegram comparten la sesión "brian"
    (memoria unificada), pero cada mensaje recuerda su origen para trazabilidad.

    SCOPE de memoria multi-usuario (H8 S10c, default PRIVADO — decisión de diseño):
      owner_user_id: de quién es este recuerdo (privado). None = legado/dueño.
      equipo_id: si no es None → recuerdo COMÚN del equipo (lo ven todos sus
        miembros). Si es None → privado de owner_user_id.
      telegram_user_id: user_id crudo de QUIÉN mandó el turno (#3, hilo por usuario).
        Traza al autor aunque cambie el formato del session_id. None = legado/CLI.
    Compat: si ambos van None (modo single-owner de hoy), se comporta igual que
    siempre.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # H6: NO filtrar deleted_at aquí — el seq debe ser monotónico aunque
            # haya episodios soft-deleted, o colisionaría el UNIQUE(session_id, seq).
            next_seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM episodes_events WHERE session_id = $1",
                session_id,
            )
            await conn.execute(
                """
                INSERT INTO episodes_events
                    (session_id, seq, role, content, tokens_in, tokens_out,
                     model, channel, owner_user_id, equipo_id, telegram_user_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                session_id,
                next_seq,
                role,
                content,
                tokens_in,
                tokens_out,
                model,
                channel,
                owner_user_id,
                equipo_id,
                telegram_user_id,
            )
    return next_seq


async def load_history(
    pool: asyncpg.Pool, session_id: str, *, last_n: int | None = None
) -> list[Turn]:
    """Reconstruye el historial de la sesión en orden cronológico.

    last_n: si se da, devuelve solo los ÚLTIMOS n turnos (en orden). Esencial
    para NO re-mandar todo el historial a Claude cada vez — sesiones largas
    (ej. 34 turnos / 96k chars) hacían que Claude tardara minutos y el bot se
    colgara. El truncado/resumen inteligente completo es R3/H5; esto es el
    tope simple de robustez.
    """
    # D-1: traemos también created_at + telegram_user_id (cuándo y quién) para que
    # el agente se oriente por tiempo y autor. Columnas existentes en la tabla.
    if last_n is not None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content, created_at, telegram_user_id "
                "FROM episodes_events WHERE session_id = $1 "
                "AND deleted_at IS NULL "  # H6: nunca devolver episodios olvidados (soft-delete)
                "ORDER BY seq DESC LIMIT $2",
                session_id,
                last_n,
            )
        rows = list(reversed(rows))  # volver a orden cronológico
    else:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content, created_at, telegram_user_id "
                "FROM episodes_events WHERE session_id = $1 "
                "AND deleted_at IS NULL "  # H6: nunca devolver episodios olvidados (soft-delete)
                "ORDER BY seq ASC",
                session_id,
            )
    return [
        Turn(
            role=r["role"],
            content=r["content"],
            created_at=r["created_at"],
            telegram_user_id=r["telegram_user_id"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class RecuerdoRelevante:
    """Un recuerdo recuperado por BÚSQUEDA SEMÁNTICA (H5). A diferencia de Turn,
    trae la distancia (qué tan relevante: 0=idéntico, mayor=menos parecido) y el
    seq (cuándo fue), para que el caller pueda umbralizar/ordenar."""

    role: str
    content: str
    seq: int
    distancia: float
    created_at: object = None  # datetime del episodio (para el "mapa de cuándo se dijo qué")


async def buscar_semantico(
    pool: asyncpg.Pool,
    session_id: str,
    query: str,
    *,
    top_n: int = 5,
    excluir_ultimos: int = 0,
    solo_usuario: bool = False,
    solo_asistente: bool = False,
    scope_user_id: int | None = None,
) -> list[RecuerdoRelevante]:
    """Busca en la memoria de la sesión los turnos más parecidos por SIGNIFICADO
    a `query` (no por palabra exacta). Usa el embedding de la query + distancia
    coseno sobre la columna `embedding` (índice HNSW). SOLO LECTURA (H5 sub-paso 6).

    top_n: cuántos recuerdos devolver (los más cercanos).
    excluir_ultimos: ignora los N turnos más recientes de la sesión — esos ya
        entran por load_history (ventana reciente); evita duplicarlos al integrar.
    solo_usuario: si True, solo considera turnos del USUARIO (role='user'), NO las
        respuestas del bot. 2026-06-19: evita el BUCLE de confirmación — el
        bot recuperaba su propia negación vieja ("no hemos hablado de eso") como
        recuerdo y la repetía. Las preguntas del usuario son la mejor señal de qué
        se habló, sin contaminar con respuestas previas del propio bot.
    scope_user_id: SCOPE multi-usuario (H8 S10c). Si se da, esta persona solo ve:
        su memoria PRIVADA (owner_user_id = scope_user_id) + la COMÚN del equipo
        (equipo_id no nulo) + el legado del dueño (owner_user_id NULL, recuerdos
        previos al equipo). NUNCA la privada de OTRA persona. Si es None → sin
        filtro de scope (modo single-owner de hoy: el dueño ve todo).

    DEFENSIVA: si el modelo de embeddings falla, devuelve [] (degrada a "sin
    recuerdos semánticos"), NO rompe el turno. Solo considera turnos con embedding
    (los que tienen NULL — si los hubiera — se ignoran).
    """
    # import perezoso: cargar el modelo (pesado) solo cuando de verdad se busca,
    # no al importar el módulo memory (que el bot importa siempre).
    try:
        from for3s_core import embeddings

        qvec = embeddings.a_pgvector(embeddings.embed(query))
    except Exception:  # noqa: BLE001 — sin embeddings, degrada a sin recuerdos
        return []

    # WHERE dinámico: base (sesión + tiene embedding + vivo) + filtros opcionales.
    # H6: "deleted_at IS NULL" → nunca recuperar como recuerdo un episodio olvidado.
    where = ["session_id = $1", "embedding IS NOT NULL", "deleted_at IS NULL"]
    params = [session_id, qvec, top_n]  # $1, $2 (vector), $3 (limit)
    if solo_usuario:
        where.append("role = 'user'")
    # solo_asistente: solo respuestas del bot (donde vive la INFO: repos, hallazgos,
    # datos). Útil para preguntas "¿qué analizamos?" donde las preguntas del usuario
    # son ruido (se parecen entre sí pero no aportan info). 2026-06-22.
    if solo_asistente:
        where.append("role = 'assistant'")
    # SCOPE multi-usuario: la persona ve SU privada + la común del equipo + el
    # legado del dueño (NULL). Nunca la privada de otro. (H8 S10c, fail-closed
    # por construcción: sin esta cláusula nadie vería de más, con ella se acota.)
    if scope_user_id is not None:
        params.append(scope_user_id)
        where.append(
            f"(owner_user_id = ${len(params)} OR equipo_id IS NOT NULL OR owner_user_id IS NULL)"
        )
    if excluir_ultimos > 0:
        # H6: NO filtrar deleted_at — es aritmética de seq para la ventana reciente;
        # debe contar contra el seq máximo real, no contra el de vivos.
        max_seq = await pool.fetchval(
            "SELECT COALESCE(MAX(seq), 0) FROM episodes_events WHERE session_id = $1",
            session_id,
        )
        params.append(max_seq - excluir_ultimos)
        where.append(f"seq <= ${len(params)}")

    sql = (
        "SELECT role, content, seq, created_at, embedding <=> $2::vector AS dist "
        "FROM episodes_events WHERE " + " AND ".join(where) + " ORDER BY dist LIMIT $3"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    recuerdos = [
        RecuerdoRelevante(
            role=r["role"],
            content=r["content"],
            seq=r["seq"],
            distancia=float(r["dist"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]
    # H6 Sub-paso 3: refrescar last_accessed de los recuerdos recuperados, en
    # BACKGROUND (fire-and-forget) — "usar" un recuerdo lo protege del olvido.
    # NUNCA debe añadir latencia ni romper la búsqueda (defensiva).
    if recuerdos:
        _tocar_recuerdos_bg(pool, session_id, [r.seq for r in recuerdos])
    return recuerdos


async def embeddear_turno(
    pool: asyncpg.Pool,
    session_id: str,
    seq: int,
    content: str,
) -> bool:
    """Genera el embedding de UN turno ya guardado y lo escribe en su columna
    (H5 sub-paso 8 pieza B). Pensada para correr en BACKGROUND (fire-and-forget):
    BGE-M3 tarda ~3s en CPU, NO debe bloquear la respuesta del bot.

    El embedding se calcula en un thread (to_thread) para no congelar el event
    loop. Solo hace UPDATE de la columna embedding del turno (session_id, seq) —
    no toca content/role/seq. Si el turno ya tiene embedding, lo recalcula (idempotente).

    DEFENSIVA: cualquier error se traga (el turno queda con embedding NULL, igual
    que antes — recuperable por backfill). NUNCA rompe el flujo que la disparó.
    Devuelve True si guardó el embedding, False si degradó.
    """
    try:
        from for3s_core import embeddings

        if not (content or "").strip():
            return False  # nada que embeber (turno vacío)
        # embed es CPU-intensivo y síncrono → a un thread para no bloquear el loop
        vec = await asyncio.to_thread(embeddings.embed, content)
        pgvec = embeddings.a_pgvector(vec)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE episodes_events SET embedding = $1::vector "
                "WHERE session_id = $2 AND seq = $3",
                pgvec,
                session_id,
                seq,
            )
        return True
    except Exception:  # noqa: BLE001 — background secundario, jamás rompe el turno
        return False


async def tocar_recuerdos(
    pool: asyncpg.Pool,
    session_id: str,
    seqs: list[int],
) -> int:
    """Marca last_accessed=now() y SUMA 1 a veces_recuperado para los episodios (seq)
    recuperados como recuerdo (H6 Sub-paso 3 + relevance v2). "Usar" un recuerdo lo
    refresca Y cuenta el uso → lo muy recuperado resiste mejor el olvido (refuerzo
    por uso real, no neutro como en la v1).

    Solo toca episodios vivos. Defensiva: cualquier error se traga (es secundario).
    Devuelve cuántas filas tocó.
    """
    try:
        if not seqs:
            return 0
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE episodes_events SET last_accessed = now(), "
                "veces_recuperado = veces_recuperado + 1 "
                "WHERE session_id = $1 AND seq = ANY($2::int[]) AND deleted_at IS NULL",
                session_id,
                seqs,
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0
    except Exception:  # noqa: BLE001 — secundario, jamás rompe la búsqueda
        return 0


async def marcar_consolidados(
    pool: asyncpg.Pool,
    session_id: str,
    seqs: list[int],
) -> int:
    """Marca consolidated_to_kg=true para los episodios (seq) cuya lección YA quedó
    escrita en el grafo (H6 Sub-paso 7, lo llama el orquestador CLS).

    ⚠️ CRÍTICO: solo se debe llamar DESPUÉS de que el concepto del cluster se
    escribió con éxito al grafo. Este flag es la condición que la Microglía exigirá
    para poder olvidar un episodio → marcarlo sin haber consolidado = riesgo de
    pérdida. Solo toca episodios vivos. Devuelve cuántas filas marcó.
    """
    if not seqs:
        return 0
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE episodes_events SET consolidated_to_kg = true "
            "WHERE session_id = $1 AND seq = ANY($2::int[]) AND deleted_at IS NULL",
            session_id,
            seqs,
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


def _tocar_recuerdos_bg(pool: asyncpg.Pool, session_id: str, seqs: list[int]) -> None:
    """Dispara tocar_recuerdos en BACKGROUND (fire-and-forget), sin esperar — para
    no añadir latencia a buscar_semantico. Retiene la ref de la tarea en _BG_TOCAR.
    """
    try:
        task = asyncio.create_task(tocar_recuerdos(pool, session_id, seqs))
        _BG_TOCAR.add(task)
        task.add_done_callback(_BG_TOCAR.discard)
    except RuntimeError:
        # sin event loop corriendo (ej. test sync) → simplemente no refresca
        pass


async def set_last_repo(pool: asyncpg.Pool, session_id: str, owner: str, repo: str) -> None:
    """Recuerda el último owner/repo de GitHub visto en la sesión (sessions.meta).

    Permite resolver referencias cortas como "el PR 134" sin URL completo.
    Se guarda en sessions.meta (JSONB) bajo la clave 'last_repo'.
    """
    await ensure_session(pool, session_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET meta = jsonb_set(meta, '{last_repo}', $2::jsonb) WHERE id = $1",
            session_id,
            json.dumps({"owner": owner, "repo": repo}),
        )


async def get_last_repo(pool: asyncpg.Pool, session_id: str) -> tuple[str, str] | None:
    """Devuelve (owner, repo) del último repo visto en la sesión, o None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT meta -> 'last_repo' AS lr FROM sessions WHERE id = $1", session_id
        )
    if not row or row["lr"] is None:
        return None
    lr = row["lr"]
    if isinstance(lr, str):  # asyncpg puede devolver JSONB como str
        lr = json.loads(lr)
    owner, repo = lr.get("owner"), lr.get("repo")
    return (owner, repo) if owner and repo else None


async def repos_analizados(
    pool: asyncpg.Pool,
    session_id: str,
    *,
    limite: int = 30,
) -> list[tuple[str, str]]:
    """G6 (2026-06-24): lista los repos REALES analizados en este hilo, leídos
    de gh_resources (el registro de lo que las tools de GitHub trajeron). Cierra el
    hueco "el bot tiene 16 repos guardados pero solo recordaba 2 de su memoria
    semántica". Devuelve [(owner, repo), ...] por recencia. DEFENSIVA: ante error, []."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT owner, repo, max(fetched_at) u FROM gh_resources "
                "WHERE session_id = $1 AND owner IS NOT NULL AND owner <> '' "
                "AND repo IS NOT NULL AND repo <> '' "
                "GROUP BY owner, repo ORDER BY u DESC LIMIT $2",
                session_id,
                limite,
            )
        return [(r["owner"], r["repo"]) for r in rows]
    except Exception:  # noqa: BLE001 — listar repos nunca rompe el turno
        return []


async def set_progreso_pendiente(
    pool: asyncpg.Pool,
    session_id: str,
    owner: str,
    repo: str,
    faltantes: list[str],
    profundo: bool,
) -> None:
    """Guarda el progreso de un mapeo que se CORTÓ por tiempo (2026-06-17):
    el repo + los archivos que NO se alcanzaron a leer. Permite que 'continúa'
    retome el mapeo REAL de lo que faltó (no improvisar). En sessions.meta.
    """
    await ensure_session(pool, session_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET meta = jsonb_set(meta, '{progreso_pendiente}', $2::jsonb)"
            " WHERE id = $1",
            session_id,
            json.dumps(
                {"owner": owner, "repo": repo, "faltantes": faltantes[:200], "profundo": profundo}
            ),
        )


async def get_progreso_pendiente(pool: asyncpg.Pool, session_id: str) -> dict | None:
    """Devuelve {owner, repo, faltantes, profundo} si hay un mapeo cortado
    pendiente de continuar, o None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT meta -> 'progreso_pendiente' AS pp FROM sessions WHERE id = $1", session_id
        )
    if not row or row["pp"] is None:
        return None
    pp = row["pp"]
    if isinstance(pp, str):
        pp = json.loads(pp)
    return pp if pp.get("faltantes") else None


async def limpiar_progreso_pendiente(pool: asyncpg.Pool, session_id: str) -> None:
    """Borra el progreso pendiente (tras completar la continuación)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET meta = meta - 'progreso_pendiente' WHERE id = $1", session_id
        )


# Mapa: nombre de tool MCP → kind de gh_resources. Ahora TODAS las tools de
# lectura se persisten (no solo las de recurso único): los read como su tipo,
# los listados como 'list', la búsqueda como 'search'. Así queda consultable
# todo lo que el agente trajo de GitHub (para los H futuros).
_TOOL_KIND = {
    "issue_read": "issue",
    "pull_request_read": "pr",
    "get_file_contents": "file",
    "list_issues": "list",
    "list_pull_requests": "list",
    "search_code": "search",
}


async def save_gh_tool_calls(
    pool: asyncpg.Pool,
    *,
    session_id: str,
    tool_calls: list[dict],
    workspace_id: str = "default",
) -> int:
    """Persiste en gh_resources/gh_files lo que las tools de GitHub trajeron.

    tool_calls: [{name, args, result}] del loop. Parsea el JSON del result
    (formato GitHub MCP) y guarda un snapshot. Defensivo: si una tool no se
    puede parsear, la salta (no rompe el turno). Devuelve cuántos recursos guardó.
    """
    guardados = 0
    for tc in tool_calls:
        kind = _TOOL_KIND.get(tc.get("name", ""))
        if kind is None:
            continue
        raw_result = tc.get("result") or ""
        try:
            data = json.loads(raw_result)
        except (ValueError, TypeError):
            data = None
        args = tc.get("args", {})
        owner = args.get("owner") or ""
        repo = args.get("repo") or ""

        # Campos por tipo. Los read (issue/pr/file) traen un dict con detalle;
        # los list/search traen una lista (o dict con lista) → guardamos un
        # resumen + el raw completo. Defensivo ante formatos variados.
        title = body = author = state = path = None
        number = None
        if isinstance(data, dict):
            owner = owner or data.get("owner") or ""
            repo = repo or data.get("repo") or ""
            title = data.get("title")
            user = data.get("user")
            author = user.get("login") if isinstance(user, dict) else data.get("author")
            state = data.get("state")
            body = data.get("body")
            n = args.get("issue_number") or args.get("pull_number") or args.get("pullNumber")
            try:
                number = int(n) if n is not None else None
            except (ValueError, TypeError):
                number = None
        if kind in ("list", "search"):
            # resumen legible del listado/búsqueda (cuántos resultados trajo)
            n_items = (
                len(data)
                if isinstance(data, list)
                else (len(data.get("items", [])) if isinstance(data, dict) else 0)
            )
            title = f"{tc.get('name')} → {n_items} resultados"
            path = args.get("path")
        if kind == "file" and not body:
            # get_file_contents: el contenido del archivo suele venir como TEXTO
            # plano (no JSON, ej. un README). Lo guardamos como body.
            body = raw_result

        # La columna raw es JSONB → SIEMPRE pasar JSON válido. Si el result no
        # parseó (texto plano como un README), lo envolvemos. Evita el error
        # "invalid input syntax for type json".
        if data is not None:
            raw_json = json.dumps(data)[:50_000]
        else:
            raw_json = json.dumps({"raw_text": raw_result[:50_000]})

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gh_resources
                    (workspace_id, session_id, kind, owner, repo, number, path,
                     title, author, state, body, raw)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                """,
                workspace_id,
                session_id,
                kind,
                owner,
                repo,
                number,
                path or args.get("path"),
                title,
                author,
                state,
                (body or "")[:8000],
                raw_json,  # SIEMPRE JSON válido (envuelto si era texto plano)
            )
        guardados += 1

        # KNOWLEDGE GRAPH (H5 sub-paso 8 pieza C): registrar el recurso en el grafo
        # además de gh_resources. Así el KG nace con datos reales conforme el bot
        # trabaja. try/except PROPIO + import lazy: si el grafo falla, JAMÁS afecta
        # el guardado de gh_resources (que es lo importante). kg.py ya es defensivo
        # e idempotente (MERGE); esto es doble seguridad.
        if owner and repo:
            try:
                from for3s_core import kg

                await kg.registrar_repo(pool, owner, repo)
                if kind in ("issue", "pr") and number is not None:
                    await kg.registrar_recurso(pool, owner, repo, kind, number, title or "")
            except Exception:  # noqa: BLE001 — KG secundario, no toca el guardado
                pass
    return guardados


# ── Apartados ARCHIVOS y WEB consultados (2026-06-19, migración 006) ──
# Registro LIGERO de qué documentos y qué páginas web ha mandado el usuario.
# SOLO metadatos + resumen (NUNCA el binario/HTML). Defensivo: si falla, NO
# rompe el turno (es un registro secundario, no crítico). Cada fila lleva su
# consulted_at (cuándo) — clave para el panorama de cómo se aloja la info.

# Tope del resumen/descripción que guardamos (corto, no volcar análisis enteros)
_MAX_RESUMEN = 2000


async def save_consulted_file(
    pool: asyncpg.Pool,
    *,
    session_id: str,
    tipo: str,
    nombre: str,
    resumen: str = "",
    workspace_id: str = "default",
) -> None:
    """Guarda un archivo consultado: tipo + nombre + resumen + cuándo. SIN el
    binario. Defensivo: cualquier error se traga (no debe tumbar el turno)."""
    try:
        await ensure_session(pool, session_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO consulted_files (workspace_id, session_id, tipo, nombre, resumen) "
                "VALUES ($1, $2, $3, $4, $5)",
                workspace_id,
                session_id,
                tipo,
                nombre,
                (resumen or "")[:_MAX_RESUMEN],
            )
    except Exception:  # noqa: BLE001 — registro secundario, no crítico
        pass


async def save_consulted_web(
    pool: asyncpg.Pool,
    *,
    session_id: str,
    url: str,
    titulo: str = "",
    descripcion: str = "",
    workspace_id: str = "default",
) -> None:
    """Guarda una página web consultada: url + título + descripción + cuándo.
    SIN el HTML. Defensivo: cualquier error se traga."""
    try:
        await ensure_session(pool, session_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO consulted_web (workspace_id, session_id, url, titulo, descripcion) "
                "VALUES ($1, $2, $3, $4, $5)",
                workspace_id,
                session_id,
                url,
                (titulo or "")[:500],
                (descripcion or "")[:_MAX_RESUMEN],
            )
    except Exception:  # noqa: BLE001
        pass
