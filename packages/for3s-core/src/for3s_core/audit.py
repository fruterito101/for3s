"""Audit chain inmutable de For3s OS (H2) — Grafo Maestro §6.4.

Cada decisión deja una entrada encadenada criptográficamente: hash_self =
SHA-256(hash_prev + contenido). Si alguien altera una entrada vieja, todos
los hashes siguientes dejan de cuadrar → manipulación detectable. La tabla
además tiene un trigger que BLOQUEA UPDATE/DELETE (append-only real).

"Esto es lo que hace For3s defendible enterprise." — Grafo §6.4
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import asyncpg

GENESIS_HASH = "0" * 64  # hash_prev de la primera entrada


def compute_hash(
    hash_prev: str, ts: str, workspace_id: str, actor: str, action: str, detail: dict
) -> str:
    """hash_self = SHA-256 del eslabón (incluye el hash anterior → cadena)."""
    payload = json.dumps(
        {
            "hash_prev": hash_prev,
            "ts": ts,
            "workspace_id": workspace_id,
            "actor": actor,
            "action": action,
            "detail": detail,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def append(
    pool: asyncpg.Pool,
    *,
    actor: str,
    action: str,
    detail: dict | None = None,
    workspace_id: str = "default",
) -> str:
    """Añade una entrada al audit chain. Devuelve su hash_self."""
    detail = detail or {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            prev = await conn.fetchval(
                "SELECT hash_self FROM audit_events ORDER BY id DESC LIMIT 1"
            )
            hash_prev = prev or GENESIS_HASH
            # ts canónico (datetime real de Postgres) para hash reproducible
            ts_dt: datetime = await conn.fetchval("SELECT now()")
            hash_self = compute_hash(
                hash_prev, ts_dt.isoformat(), workspace_id, actor, action, detail
            )
            await conn.execute(
                """
                INSERT INTO audit_events
                    (ts, workspace_id, actor, action, detail, hash_prev, hash_self)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                """,
                ts_dt,
                workspace_id,
                actor,
                action,
                json.dumps(detail),
                hash_prev,
                hash_self,
            )
    return hash_self


async def verify_chain(pool: asyncpg.Pool) -> tuple[bool, int]:
    """Recorre toda la cadena y verifica integridad.

    Devuelve (es_íntegra, num_entradas). Si algún hash no cuadra → False.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts, workspace_id, actor, action, detail, hash_prev, hash_self "
            "FROM audit_events ORDER BY id ASC"
        )
    expected_prev = GENESIS_HASH
    for r in rows:
        if r["hash_prev"] != expected_prev:
            return False, len(rows)
        recomputed = compute_hash(
            r["hash_prev"],
            r["ts"].isoformat(),
            r["workspace_id"],
            r["actor"],
            r["action"],
            json.loads(r["detail"]),
        )
        if recomputed != r["hash_self"]:
            return False, len(rows)
        expected_prev = r["hash_self"]
    return True, len(rows)
