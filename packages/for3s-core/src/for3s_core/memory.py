"""Memoria episódica de For3s OS (H2) — Nodo 2 Hipocampo (versión cruda).

Event Sourcing: cada turno (user dice X, For3s responde Y) es un evento
append-only en episodes_events. El historial de una sesión se reconstruye
leyendo esos eventos en orden → For3s "recuerda" entre reinicios.

(Búsqueda semántica, KG, olvido y consolidación llegan en H5/H6.)
"""

from __future__ import annotations

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
) -> int:
    """Guarda un turno como evento append-only. Devuelve su seq."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM episodes_events WHERE session_id = $1",
                session_id,
            )
            await conn.execute(
                """
                INSERT INTO episodes_events
                    (session_id, seq, role, content, tokens_in, tokens_out, model)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                session_id,
                next_seq,
                role,
                content,
                tokens_in,
                tokens_out,
                model,
            )
    return next_seq


async def load_history(pool: asyncpg.Pool, session_id: str) -> list[Turn]:
    """Reconstruye el historial COMPLETO de la sesión en orden cronológico."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content FROM episodes_events WHERE session_id = $1 ORDER BY seq ASC",
            session_id,
        )
    return [Turn(role=r["role"], content=r["content"]) for r in rows]
