# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — TEMAS por persona (AI2, 2026-06-23).

Un mismo chat de Telegram (una persona) puede tener VARIOS temas/hilos separados,
cada uno con su propia conversación. Solo UNO activo a la vez por persona. El
conocimiento (grafo/CLS) se sigue compartiendo; lo que se separa es el HILO de
conversación.

El session_id de cada turno pasa a ser "tg:<user_id>:<tema>" (extiende el #6).
Default = "general" → OPT-IN: sin usar /tema, todo va a "general" (= como hoy).
ADITIVO y fail-safe: ante cualquier error, se cae a "general".
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC

logger = logging.getLogger("for3s.temas")

TEMA_DEFAULT = "general"
_MAX_LEN = 30  # tope de longitud del nombre de tema


@dataclass(frozen=True)
class HiloInfo:
    """Un hilo (tema) de una persona, con su actividad — para /hilos (AI7)."""

    nombre: str
    activo: bool
    ultimo_uso: object | None  # timestamp del último turno (o None si vacío)
    turnos: int  # cuántos turnos vivos tiene ese hilo


def normalizar_nombre(nombre: str) -> str:
    """Convierte el nombre que escribe el usuario en un slug seguro y consistente.
    quita acentos (Día→dia), minúsculas, espacios→guiones, solo [a-z0-9-],
    recortado. '' → 'general'."""
    s = (nombre or "").strip().lower()
    # transliterar acentos: 'día'→'dia', 'ñ'→'n' (NFKD + descartar diacríticos)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:_MAX_LEN] or TEMA_DEFAULT


class TemaStore:
    """Store de temas por persona sobre PostgreSQL (asyncpg, sin ORM)."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def activo(self, user_id: int) -> str:
        """Devuelve el nombre del tema ACTIVO de la persona. Si no tiene ninguno
        marcado activo → 'general' (default, sin tocar la BD). Fail-safe: ante
        error devuelve 'general' para no romper el flujo del bot."""
        try:
            async with self._pool.acquire() as con:
                t = await con.fetchval(
                    "SELECT nombre FROM temas WHERE user_id = $1 AND activo",
                    user_id,
                )
            return t or TEMA_DEFAULT
        except Exception:  # noqa: BLE001 — los temas nunca deben tumbar el bot
            logger.warning("error leyendo tema activo (uso 'general')", exc_info=True)
            return TEMA_DEFAULT

    async def cambiar(self, user_id: int, nombre: str) -> str:
        """Crea (si no existe) y ACTIVA un tema para la persona. Desactiva el resto.
        Devuelve el slug normalizado del tema activado. Idempotente."""
        slug = normalizar_nombre(nombre)
        async with self._pool.acquire() as con:
            async with con.transaction():
                # desactivar todos los de esta persona
                await con.execute(
                    "UPDATE temas SET activo = false WHERE user_id = $1 AND activo",
                    user_id,
                )
                # crear/activar el elegido (upsert)
                await con.execute(
                    "INSERT INTO temas (user_id, nombre, activo) VALUES ($1, $2, true) "
                    "ON CONFLICT (user_id, nombre) DO UPDATE "
                    "SET activo = true, ultimo_uso = now()",
                    user_id,
                    slug,
                )
        logger.info("[temas] user=%s -> tema activo '%s'", user_id, slug)
        return slug

    async def listar(self, user_id: int) -> list[tuple[str, bool]]:
        """Lista los temas de la persona como (nombre, es_activo), por uso reciente."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT nombre, activo FROM temas WHERE user_id = $1 "
                "ORDER BY activo DESC, ultimo_uso DESC",
                user_id,
            )
        return [(r["nombre"], r["activo"]) for r in rows]

    async def resumen_hilos(self, user_id: int, base_sesion: str) -> list[HiloInfo]:
        """AI7 — vista de los HILOS de una persona para /hilos: cada tema + su
        actividad real (último turno + nº de turnos vivos), leída de episodes_events.
        Incluye SIEMPRE el hilo 'general' (= base_sesion), aunque no haya fila en
        `temas` (es el default implícito). base_sesion lo arma el canal con la misma
        convención que _sesion_de (general→base, otros→base:tema) para que el
        session_id coincida. Ordena: activo primero, luego por actividad reciente."""
        temas = await self.listar(user_id)
        nombres = [n for n, _ in temas]
        activos = {n for n, a in temas if a}
        # garantizar 'general' presente (default implícito, sin fila en temas)
        if TEMA_DEFAULT not in nombres:
            nombres.insert(0, TEMA_DEFAULT)
            # si nadie está marcado activo, el activo de facto es 'general'
            if not activos:
                activos.add(TEMA_DEFAULT)

        infos: list[HiloInfo] = []
        async with self._pool.acquire() as con:
            for nombre in nombres:
                sid = base_sesion if nombre == TEMA_DEFAULT else f"{base_sesion}:{nombre}"
                row = await con.fetchrow(
                    "SELECT max(created_at) AS ult, count(*) AS n "
                    "FROM episodes_events WHERE session_id = $1 AND deleted_at IS NULL",
                    sid,
                )
                infos.append(
                    HiloInfo(
                        nombre=nombre,
                        activo=nombre in activos,
                        ultimo_uso=row["ult"] if row else None,
                        turnos=row["n"] if row else 0,
                    )
                )
        # orden: activo primero, luego por último uso desc (None al final)
        from datetime import datetime

        _epoch = datetime(1970, 1, 1, tzinfo=UTC)
        infos.sort(key=lambda h: (not h.activo, -(h.ultimo_uso or _epoch).timestamp()))
        return infos
