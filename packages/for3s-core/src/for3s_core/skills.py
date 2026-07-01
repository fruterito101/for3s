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
        """Crea (o reemplaza) una skill. Devuelve su id. Idempotente por (cat,nombre).

        HA-5: genera el EMBEDDING (nombre+descripción+tags) para el matcher semántico
        (buscar_relevantes). Defensivo: si el modelo no está, la skill se crea con
        embedding NULL (el matcher cae al fallback por palabras para esa skill; el
        backfill la rellena después)."""
        slug = normalizar_nombre(nombre)
        emb = None
        try:
            from for3s_core import embeddings

            texto_emb = f"{slug.replace('-', ' ')}. {descripcion}. {' '.join(tags or [])}"
            emb = embeddings.a_pgvector(embeddings.embed(texto_emb))
        except Exception:  # noqa: BLE001 — sin modelo → embedding NULL, no rompe
            logger.warning("[skills] no pude generar embedding para %s (queda NULL)", slug)
        async with self._pool.acquire() as con:
            sid = await con.fetchval(
                "INSERT INTO skills (nombre, categoria, descripcion, contenido, tags, "
                " provenance, creada_por, embedding) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector) "
                "ON CONFLICT (categoria, nombre) DO UPDATE "
                "SET contenido=$4, descripcion=$3, tags=$5, actualizada_at=now(), "
                "    lifecycle='active', embedding=COALESCE($8::vector, skills.embedding) "
                "RETURNING id",
                slug,
                categoria,
                descripcion[:200],
                contenido,
                json.dumps(tags or []),
                provenance,
                creada_por,
                emb,
            )
        logger.info("[skills] creada/actualizada %s/%s (prov=%s, emb=%s)",
                    categoria, slug, provenance, "sí" if emb else "no")
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

    # Stopwords: palabras demasiado comunes para que el FALLBACK por palabras cuente
    # como señal (evita que "logs del servidor" dispare la skill de deploy).
    _STOPWORDS_SKILL = frozenset({
        "para", "como", "cuando", "donde", "esta", "este", "esto", "eso", "tienes",
        "puedes", "quiero", "necesito", "ayuda", "ayudame", "hacer", "sobre", "todo",
        "algo", "tengo", "estoy", "vamos", "favor", "porfa", "servidor", "server",
    })
    # Umbral de distancia coseno: una skill APLICA si su embedding está a distancia
    # < este corte del mensaje. BGE-M3: ~0.0 idéntico, ~1.0 nada que ver. 0.55 =
    # "del mismo tema" sin disparar por cualquier roce.
    _UMBRAL_SKILL_DIST = 0.55

    async def buscar_relevantes(self, texto: str, *, limite: int = 3) -> list[SkillInfo]:
        """Skills que APLICAN al texto, por SIGNIFICADO (HA-5, 2026-06-30 — semántico).

        Usa el embedding BGE-M3 del mensaje vs el de cada skill (distancia coseno,
        índice HNSW), igual que la memoria semántica (H5). Resuelve los dos fallos del
        matcher por palabras: falsos positivos (1 palabra común disparaba la skill) y
        falsos negativos (cruza idiomas: 'despliego el bot' ≈ 'deploy bot servidor').
        Solo inyecta si distancia < _UMBRAL_SKILL_DIST.

        DEFENSIVO Y ADITIVO: si el modelo falla o hay skills sin embedding aún, cae al
        matcher por palabras (_buscar_relevantes_palabras). Nunca rompe el turno."""
        try:
            from for3s_core import embeddings

            qvec = embeddings.a_pgvector(embeddings.embed(texto))
        except Exception:  # noqa: BLE001 — sin modelo → fallback a palabras
            logger.warning("embeddings no disponibles para skills, uso fallback palabras")
            return await self._buscar_relevantes_palabras(texto, limite=limite)
        try:
            async with self._pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, nombre, categoria, descripcion, provenance, lifecycle, "
                    "veces_usada, embedding <=> $1::vector AS dist FROM skills "
                    "WHERE lifecycle='active' AND embedding IS NOT NULL "
                    "ORDER BY dist LIMIT $2",
                    qvec,
                    limite,
                )
            relevantes = [
                SkillInfo(
                    r["id"], r["nombre"], r["categoria"], r["descripcion"],
                    r["provenance"], r["lifecycle"], r["veces_usada"],
                )
                for r in rows
                if float(r["dist"]) < self._UMBRAL_SKILL_DIST
            ]
            if not relevantes:
                async with self._pool.acquire() as con:
                    faltan = await con.fetchval(
                        "SELECT count(*) FROM skills WHERE lifecycle='active' "
                        "AND embedding IS NULL"
                    )
                if faltan:
                    return await self._buscar_relevantes_palabras(texto, limite=limite)
            return relevantes
        except Exception:  # noqa: BLE001
            logger.warning("error en búsqueda semántica de skills (fallback)", exc_info=True)
            return await self._buscar_relevantes_palabras(texto, limite=limite)

    async def _buscar_relevantes_palabras(
        self, texto: str, *, limite: int = 3
    ) -> list[SkillInfo]:
        """FALLBACK por palabras (sin embeddings). Con umbral: ≥1 hit en el nombre o
        ≥2 en desc/tags (stopwords no puntúan). Menos preciso que el semántico pero
        mejor que el OR original (no dispara por 1 palabra suelta)."""
        try:
            t = normalizar_nombre(texto).replace("-", " ")
            palabras = {
                p for p in t.split()
                if len(p) >= 4 and p not in self._STOPWORDS_SKILL
            }
            if not palabras:
                return []
            patron = "(" + "|".join(re.escape(p) for p in palabras) + ")"
            async with self._pool.acquire() as con:
                rows = await con.fetch(
                    "SELECT id, nombre, categoria, descripcion, provenance, lifecycle, "
                    "veces_usada, lower(nombre) lname, lower(descripcion) ldesc, "
                    "lower(tags::text) ltags FROM skills WHERE lifecycle='active' AND ("
                    "  lower(nombre) ~ $1 OR lower(descripcion) ~ $1 OR lower(tags::text) ~ $1"
                    ")",
                    patron,
                )
            puntuadas = []
            for r in rows:
                nombre_norm = r["lname"].replace("-", " ")
                hits_nombre = sum(1 for p in palabras if p in nombre_norm)
                blob = f"{r['ldesc']} {r['ltags']}"
                hits_texto = sum(1 for p in palabras if p in blob)
                if hits_nombre >= 1 or hits_texto >= 2:
                    score = hits_nombre * 10 + hits_texto
                    puntuadas.append((score, r))
            puntuadas.sort(key=lambda x: (x[0], x[1]["veces_usada"]), reverse=True)
            return [
                SkillInfo(
                    r["id"], r["nombre"], r["categoria"], r["descripcion"],
                    r["provenance"], r["lifecycle"], r["veces_usada"],
                )
                for _score, r in puntuadas[:limite]
            ]
        except Exception:  # noqa: BLE001
            logger.warning("error buscando skills relevantes (ignoro)", exc_info=True)
            return []
