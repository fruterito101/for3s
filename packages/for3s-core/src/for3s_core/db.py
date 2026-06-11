"""Capa de base de datos de For3s OS (H2) — conexión async + migraciones.

R2 lockeó asyncpg. Capa de datos con SQL directo (transparente, auditable;
el audit chain es SQL puro). En vez de Alembic ORM, un runner de migraciones
SQL numeradas: lee migrations/NNN_*.sql en orden, aplica las que falten, y
registra cada una en schema_version. Esto da evolución versionada del esquema
(añadir/transformar tablas con datos adentro) sin la maquinaria del ORM.

⚠️ Desviación registrada de R2 (SQLAlchemy+Alembic) — ver Grafo §0.2.
"""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def _asyncpg_dsn(database_url: str) -> str:
    """postgresql+asyncpg://... (SQLAlchemy) → postgresql://... (asyncpg)."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def connect(database_url: str) -> asyncpg.Pool:
    """Crea un pool de conexiones a Postgres."""
    return await asyncpg.create_pool(_asyncpg_dsn(database_url), min_size=1, max_size=5)


def _discover_migrations() -> list[tuple[int, Path]]:
    """Lista migraciones (version, path) ordenadas por número."""
    found: list[tuple[int, Path]] = []
    for p in _MIGRATIONS_DIR.glob("*.sql"):
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found, key=lambda x: x[0])


async def apply_migrations(pool: asyncpg.Pool) -> list[int]:
    """Aplica migraciones pendientes en orden. Devuelve las versiones aplicadas.

    Cada migración corre dentro de una transacción; si falla, se revierte y se
    detiene (no deja el esquema a medias).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_version")}

    applied: list[int] = []
    for version, path in _discover_migrations():
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES ($1, $2)",
                    version,
                    path.name,
                )
        applied.append(version)
    return applied
