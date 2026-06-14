"""Memoria episódica de For3s OS (H2) — Nodo 2 Hipocampo (versión cruda).

Event Sourcing: cada turno (user dice X, For3s responde Y) es un evento
append-only en episodes_events. El historial de una sesión se reconstruye
leyendo esos eventos en orden → For3s "recuerda" entre reinicios.

(Búsqueda semántica, KG, olvido y consolidación llegan en H5/H6.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class Turn:
    """Un turno de conversación recuperado de la memoria."""

    role: str  # "user" | "assistant"
    content: str


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
) -> int:
    """Guarda un turno como evento append-only. Devuelve su seq.

    channel: por qué puerta entró este turno ('cli' | 'telegram'). Se guarda
    POR TURNO (no por sesión) — CLI y Telegram comparten la sesión "brian"
    (memoria unificada), pero cada mensaje recuerda su origen para trazabilidad.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM episodes_events WHERE session_id = $1",
                session_id,
            )
            await conn.execute(
                """
                INSERT INTO episodes_events
                    (session_id, seq, role, content, tokens_in, tokens_out, model, channel)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                session_id,
                next_seq,
                role,
                content,
                tokens_in,
                tokens_out,
                model,
                channel,
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
    if last_n is not None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM episodes_events WHERE session_id = $1 "
                "ORDER BY seq DESC LIMIT $2",
                session_id,
                last_n,
            )
        rows = list(reversed(rows))  # volver a orden cronológico
    else:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM episodes_events WHERE session_id = $1 ORDER BY seq ASC",
                session_id,
            )
    return [Turn(role=r["role"], content=r["content"]) for r in rows]


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


# Mapa: nombre de tool MCP → kind de gh_resources (las que traen UN recurso).
# Los listados (list_issues/list_pull_requests) NO se persisten como recurso
# único — traen muchos a medias; el valor está en leer uno (read).
_TOOL_KIND = {
    "issue_read": "issue",
    "pull_request_read": "pr",
    "get_file_contents": "file",
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
            continue  # listados/búsquedas no se persisten como recurso único
        try:
            data = json.loads(tc.get("result") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        args = tc.get("args", {})
        owner = args.get("owner") or data.get("owner") or ""
        repo = args.get("repo") or data.get("repo") or ""
        number = args.get("issue_number") or args.get("pull_number") or args.get("pullNumber")
        try:
            number = int(number) if number is not None else None
        except (ValueError, TypeError):
            number = None
        user = data.get("user")
        author = user.get("login") if isinstance(user, dict) else data.get("author")
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
                args.get("path"),
                data.get("title"),
                author,
                data.get("state"),
                (data.get("body") or "")[:8000],
                json.dumps(data)[:50_000],
            )
        guardados += 1
    return guardados
