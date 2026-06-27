# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — SKILLS (H10 "APRENDE", 2026-06-24).

Una skill = una receta reutilizable (SKILL.md: cuándo usarla + pasos) que el agente
aplica con sus tools. H10 = almacenamiento + uso manual (sin auto-generación todavía;
eso es H12, tras el governor H11). DB-backed (PostgreSQL, estilo For3s).

Carga progresiva (disciplina AI6): el LISTADO trae solo metadata (nombre, descripción);
el CONTENIDO completo (SKILL.md) se carga solo cuando una skill aplica de verdad.

DEFENSIVO: leer skills nunca rompe el turno.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("for3s.skills")

PROV_USUARIO = "usuario"  # la pidió un humano → intocable por el curator
PROV_AUTO = "auto"  # auto-generada (H12) → el governor/curator la gestiona


def normalizar_nombre(nombre: str) -> str:
    """slug seguro: minúsculas, sin acentos, espacios→guiones, [a-z0-9-]."""
    import unicodedata

    s = (nombre or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:64]


@dataclass(frozen=True)
class SkillInfo:
    """Metadata de una skill (para el listado — sin el contenido completo)."""

    id: int
    nombre: str
    categoria: str
    descripcion: str
    provenance: str
    lifecycle: str
    veces_usada: int


class SkillStore:
    """Store de skills sobre PostgreSQL (asyncpg, sin ORM)."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def crear(
        self,
        nombre: str,
        contenido: str,
        *,
        categoria: str = "general",
        descripcion: str = "",
        tags: list[str] | None = None,
        provenance: str = PROV_USUARIO,
        creada_por: int | None = None,
    ) -> int:
        """Crea (o reemplaza) una skill. Devuelve su id. Idempotente por (cat,nombre)."""
        slug = normalizar_nombre(nombre)
        async with self._pool.acquire() as con:
            sid = await con.fetchval(
                "INSERT INTO skills (nombre, categoria, descripcion, contenido, tags, "
                " provenance, creada_por) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (categoria, nombre) DO UPDATE "
                "SET contenido=$4, descripcion=$3, tags=$5, actualizada_at=now(), "
                "    lifecycle='active' "
                "RETURNING id",
                slug,
                categoria,
                descripcion[:200],
                contenido,
                json.dumps(tags or []),
                provenance,
                creada_por,
            )
        logger.info("[skills] creada/actualizada %s/%s (prov=%s)", categoria, slug, provenance)
        return sid

    async def listar(self, *, solo_activas: bool = True) -> list[SkillInfo]:
        """Lista skills (metadata, sin contenido). Por defecto solo las activas."""
        try:
            q = (
                "SELECT id, nombre, categoria, descripcion, provenance, lifecycle, "
                "veces_usada FROM skills"
            )
            if solo_activas:
                q += " WHERE lifecycle = 'active'"
            q += " ORDER BY categoria, nombre"
            async with self._pool.acquire() as con:
                rows = await con.fetch(q)
            return [
                SkillInfo(
                    r["id"],
                    r["nombre"],
                    r["categoria"],
                    r["descripcion"],
                    r["provenance"],
                    r["lifecycle"],
                    r["veces_usada"],
                )
                for r in rows
            ]
        except Exception:  # noqa: BLE001 — listar skills nunca rompe el turno
            logger.warning("error listando skills (ignoro)", exc_info=True)
            return []

    async def ver(self, nombre: str, *, categoria: str | None = None) -> dict | None:
        """Devuelve la skill COMPLETA (con contenido) por nombre. None si no existe."""
        slug = normalizar_nombre(nombre)
        async with self._pool.acquire() as con:
            if categoria:
                r = await con.fetchrow(
                    "SELECT * FROM skills WHERE categoria=$1 AND nombre=$2", categoria, slug
                )
            else:
                r = await con.fetchrow(
                    "SELECT * FROM skills WHERE nombre=$1 AND lifecycle='active' "
                    "ORDER BY actualizada_at DESC LIMIT 1",
                    slug,
                )
        return dict(r) if r else None

    async def registrar_uso(self, skill_id: int) -> None:
        """Incrementa el contador de uso + refresca ultimo_uso (para la curación
        nocturna: lo usado resiste el archivado). Fire-and-forget, defensivo."""
        try:
            async with self._pool.acquire() as con:
                await con.execute(
                    "UPDATE skills SET veces_usada = veces_usada + 1, ultimo_uso = now() "
                    "WHERE id = $1",
                    skill_id,
                )
        except Exception:  # noqa: BLE001
            pass

    async def buscar_relevantes(self, texto: str, *, limite: int = 3) -> list[SkillInfo]:
        """Skills cuyo nombre/descripción/tags coinciden con el texto (match simple por
        palabras). Para inyectar al contexto cuando una skill APLICA (H10-c). Defensivo."""
        try:
            t = normalizar_nombre(texto).replace("-", " ")
            palabras = [p for p in t.split() if len(p) >= 4]
            if not palabras:
                return []
            async with self._pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, nombre, categoria, descripcion, provenance, lifecycle, "
                    "veces_usada FROM skills WHERE lifecycle='active' AND ("
                    "  lower(nombre) ~ $1 OR lower(descripcion) ~ $1 OR lower(tags::text) ~ $1"
                    ") ORDER BY veces_usada DESC LIMIT $2",
                    "(" + "|".join(re.escape(p) for p in palabras) + ")",
                    limite,
                )
            return [
                SkillInfo(
                    r["id"],
                    r["nombre"],
                    r["categoria"],
                    r["descripcion"],
                    r["provenance"],
                    r["lifecycle"],
                    r["veces_usada"],
                )
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            logger.warning("error buscando skills relevantes (ignoro)", exc_info=True)
            return []
