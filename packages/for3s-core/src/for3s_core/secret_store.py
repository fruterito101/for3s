# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""Almacén de secretos cifrados de For3s OS (H4) — une KEK (crypto) + BD.

API: set_secret / get_secret por (workspace, nombre). El plaintext solo
existe el instante en que se usa ("decrypt minimum", Grafo §6.2). Cada
acceso queda en el audit chain (sin exponer el valor, claro).
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

from for3s_core import audit, crypto

DEFAULT_MASTER_KEY_PATH = Path.home() / ".for3s" / "master.key"


class SecretStore:
    """Secretos cifrados por workspace (AES-256-GCM, clave derivada HKDF)."""

    def __init__(self, pool: asyncpg.Pool, master_key_path: Path | None = None) -> None:
        self._pool = pool
        self._master = crypto.load_or_create_master_key(master_key_path or DEFAULT_MASTER_KEY_PATH)

    async def set_secret(self, workspace_id: str, name: str, value: str) -> None:
        wkey = crypto.derive_workspace_key(self._master, workspace_id)
        nonce, ct = crypto.encrypt(wkey, value)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO secrets (workspace_id, name, nonce, ciphertext)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (workspace_id, name)
                DO UPDATE SET nonce = $3, ciphertext = $4, updated_at = now()
                """,
                workspace_id,
                name,
                nonce,
                ct,
            )
        await audit.append(
            self._pool,
            actor="for3s",
            action="secret_set",
            detail={"workspace": workspace_id, "name": name},  # nunca el valor
        )

    async def get_secret(self, workspace_id: str, name: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT nonce, ciphertext FROM secrets WHERE workspace_id=$1 AND name=$2",
                workspace_id,
                name,
            )
        if row is None:
            return None
        wkey = crypto.derive_workspace_key(self._master, workspace_id)
        value = crypto.decrypt(wkey, bytes(row["nonce"]), bytes(row["ciphertext"]))
        await audit.append(
            self._pool,
            actor="for3s",
            action="secret_read",
            detail={"workspace": workspace_id, "name": name},
        )
        return value
