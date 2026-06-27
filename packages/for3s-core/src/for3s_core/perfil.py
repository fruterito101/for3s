"""For3s OS — PERFIL de usuario (P1 modelar al usuario, 2026-06-24).

For3s recuerda QUÉ se habló; el perfil guarda QUIÉN es cada persona (rol, stack,
estilo, rasgos) para adaptar sus respuestas. Híbrido: la persona lo dice o el bot
infiere y confirma. Por PERSONA, global (aplica en todos sus temas).

Se inyecta al contexto al responderle (como memoria/grafo/STATUS). DEFENSIVO:
leer/guardar nunca rompe el turno.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("for3s.perfil")

# campos clave estructurados (además de los rasgos libres en JSONB)
_CAMPOS_CLAVE = ("nombre", "rol", "stack", "estilo", "zona")

# Detector de afirmaciones de identidad ("soy backend", "trabajo en frontend",
# "prefiero respuestas cortas"...). Captura explícita: el usuario lo DICE.
_PATRONES_ROL = re.compile(
    r"\b(?:soy|trabajo (?:en|como|de)|me dedico a|mi rol es)\s+(?:el |la |un |una )?"
    r"([a-záéíóúñ ]{3,40})",
    re.IGNORECASE,
)
_PATRONES_PREFERENCIA = re.compile(
    r"\b(?:prefiero|me gusta(?:n)?|odio|no me gusta(?:n)?|mejor)\s+([a-záéíóúñ ,]{3,60})",
    re.IGNORECASE,
)


def detectar_afirmacion(texto: str) -> dict | None:
    """¿El mensaje contiene una afirmación de identidad/preferencia que valga la pena
    guardar en el perfil? Devuelve {'rol':...} o {'rasgo':...} o None. CONSERVADOR:
    solo frases claras de auto-descripción, no cualquier 'soy'."""
    t = (texto or "").strip()
    if len(t) < 6:
        return None
    m = _PATRONES_ROL.search(t)
    if m:
        rol = m.group(1).strip().rstrip(".,").lower()
        # cortar en conectores: "backend y trabajo con X" → "backend" (quedarse con
        # el rol, no arrastrar el resto de la frase).
        rol = re.split(r"\s+(?:y|pero|aunque|que|porque|con|en|para|al|de|,)\s+", rol)[0].strip()
        # filtrar lo que NO es un rol real (adjetivos de personalidad, palabras vacías)
        _NO_ROL = {
            "claro",
            "honesto",
            "directo",
            "nuevo",
            "el",
            "la",
            "yo",
            "muy",
            "bueno",
            "malo",
            "feliz",
            "rapido",
            "rápido",
            "amable",
            "serio",
        }
        primera = rol.split()[0] if rol.split() else ""
        if 1 <= len(rol.split()) <= 3 and primera not in _NO_ROL:
            return {"rol": rol[:40]}
    m = _PATRONES_PREFERENCIA.search(t)
    if m:
        pref = m.group(1).strip().rstrip(".,")
        return {"rasgo": f"prefiere {pref}"[:80]}
    return None


class PerfilStore:
    """Store del perfil por persona sobre PostgreSQL (asyncpg, sin ORM)."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def get(self, user_id: int) -> dict | None:
        """Devuelve el perfil de la persona como dict, o None si no tiene."""
        try:
            async with self._pool.acquire() as con:
                r = await con.fetchrow(
                    "SELECT nombre, rol, stack, estilo, zona, rasgos "
                    "FROM perfil_usuario WHERE telegram_user_id = $1",
                    user_id,
                )
            if r is None:
                return None
            rasgos = r["rasgos"]
            if isinstance(rasgos, str):
                rasgos = json.loads(rasgos)
            return {
                "nombre": r["nombre"],
                "rol": r["rol"],
                "stack": r["stack"],
                "estilo": r["estilo"],
                "zona": r["zona"],
                "rasgos": rasgos or [],
            }
        except Exception:  # noqa: BLE001 — leer perfil nunca rompe el turno
            logger.warning("error leyendo perfil (ignoro)", exc_info=True)
            return None

    async def set_campo(
        self, user_id: int, campo: str, valor: str, *, nombre: str | None = None
    ) -> None:
        """Fija un campo CLAVE del perfil (rol/stack/estilo/zona/nombre). Upsert."""
        if campo not in _CAMPOS_CLAVE:
            return
        async with self._pool.acquire() as con:
            await con.execute(
                f"INSERT INTO perfil_usuario (telegram_user_id, {campo}, nombre) "  # noqa: S608
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (telegram_user_id) DO UPDATE "
                f"SET {campo} = $2, actualizado_at = now(), "
                "    nombre = COALESCE(perfil_usuario.nombre, $3)",
                user_id,
                valor,
                nombre,
            )
        logger.info("[perfil] user=%s set %s", user_id, campo)

    async def add_rasgo(
        self, user_id: int, rasgo: str, *, nombre: str | None = None, max_rasgos: int = 15
    ) -> None:
        """Añade un rasgo libre (sin duplicar), tope max_rasgos (los más recientes)."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                cur = await con.fetchval(
                    "SELECT rasgos FROM perfil_usuario WHERE telegram_user_id=$1", user_id
                )
                rasgos = (json.loads(cur) if isinstance(cur, str) else cur) or []
                if rasgo in rasgos:
                    return
                rasgos.append(rasgo)
                rasgos = rasgos[-max_rasgos:]
                await con.execute(
                    "INSERT INTO perfil_usuario (telegram_user_id, rasgos, nombre) "
                    "VALUES ($1, $2, $3) "
                    "ON CONFLICT (telegram_user_id) DO UPDATE "
                    "SET rasgos = $2, actualizado_at = now(), "
                    "    nombre = COALESCE(perfil_usuario.nombre, $3)",
                    user_id,
                    json.dumps(rasgos),
                    nombre,
                )
        logger.info("[perfil] user=%s +rasgo", user_id)

    async def resumen(self, user_id: int) -> str | None:
        """Texto del perfil para INYECTAR al contexto (que el bot adapte su respuesta
        a quién es). None si no hay perfil. NO inventa: solo lo declarado/aprendido."""
        p = await self.get(user_id)
        if not p:
            return None
        partes = []
        if p.get("nombre"):
            partes.append(f"nombre: {p['nombre']}")
        if p.get("rol"):
            partes.append(f"rol: {p['rol']}")
        if p.get("stack"):
            partes.append(f"stack/herramientas: {p['stack']}")
        if p.get("estilo"):
            partes.append(f"estilo preferido: {p['estilo']}")
        if p.get("zona"):
            partes.append(f"zona: {p['zona']}")
        for r in (p.get("rasgos") or [])[:8]:
            partes.append(r)
        if not partes:
            return None
        return (
            "PERFIL DE ESTA PERSONA (con quien hablas — adapta tu respuesta a "
            "quién es, sin recitarlo literal):\n- " + "\n- ".join(partes)
        )
