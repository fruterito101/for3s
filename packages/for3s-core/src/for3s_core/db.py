"""Capa de base de datos de For3s OS (H2) — conexión async a PostgreSQL.

R2 lockeó asyncpg. Aquí: pool de conexiones + aplicar el esquema. Sin ORM
pesado: SQL directo, transparente y auditable (el audit chain es SQL puro).
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _asyncpg_dsn(database_url: str) -> str:
    """Convierte la URL estilo SQLAlchemy a DSN que asyncpg entiende.

    .env trae postgresql+asyncpg://...  (formato SQLAlchemy);
    asyncpg quiere postgresql://...
    """
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def connect(database_url: str) -> asyncpg.Pool:
    """Crea un pool de conexiones a Postgres."""
    return await asyncpg.create_pool(_asyncpg_dsn(database_url), min_size=1, max_size=5)


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Aplica el esquema (idempotente). Crea tablas, índices y el trigger."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
