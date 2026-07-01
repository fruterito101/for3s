# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — MEMORIA: fachada central de la capa de memoria (F2 del REDISEÑO MEMORIA).

Ronda: Cuerpo/Ronda_Rediseno_Memoria_Plan.md §F2.

PROBLEMA (medido): la memoria vivía en 5 capas parciales SUELTAS, con 2 estilos e
identidades distintas:
  · clases por user_id:  PerfilStore.get(uid) · TemaStore.activo(uid)
  · funciones por session_id/pool:  memory.buscar_semantico(pool, sid) · kg.conceptos(pool)
    · hilo_status.get_status(pool, sid)
16 módulos las tocaban directo y conversation.py las pegaba a mano (14 accesos en send()).
Cambiar el esquema = tocar 16 sitios → no era mantenible como producto.

SOLUCIÓN (F2): esta fachada COORDINA las 5 capas (NO las reescribe — siguen siendo el
motor). Recibe la identidad canónica (telegram_user_id, F1) y TRADUCE a lo que cada capa
espera (deriva session_id con la regla canónica). Métodos por INTENCIÓN, no por tabla.

IDENTIDAD CANÓNICA (F1): PERSONA = telegram_user_id (bigint).
SESIÓN = 'tg:'||uid[:tema]. Una sola función la deriva (sesion_de) → no se inventa.

ADITIVO: no cambia las capas ni el comportamiento actual; los consumidores migran a la
fachada GRADUALMENTE. DEFENSIVO: cada sub-llamada protegida (una capa caída no rompe el resto).
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger("for3s.memoria")

TEMA_GENERAL = "general"  # el tema por defecto NO añade sufijo a la sesión


def sesion_de(uid: int, tema: str | None = None) -> str:
    """Regla CANÓNICA de sesión (F1): la sesión SIEMPRE se deriva de la persona.
    `tg:<uid>` para el tema general; `tg:<uid>:<tema>` para otros. No se inventa
    ni se parsea a mano en cada sitio — se calcula aquí, único punto."""
    base = f"tg:{uid}"
    if tema and tema != TEMA_GENERAL:
        return f"{base}:{tema}"
    return base


class Memoria:
    """Fachada única de la capa de memoria. Coordina perfil, temas, hilo_status,
    memoria semántica (memory.py) y grafo (kg.py) tras una sola identidad canónica."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def persona(self, uid: int) -> dict:
        """TODO de una persona en 1 llamada (la 'vista maestra' en código): perfil +
        tema activo + hilos + resumen de perfil. Antes había que llamar a 3 capas
        distintas y pegarlo a mano. Defensivo: cada parte protegida."""
        out: dict = {"telegram_user_id": uid}
        # perfil (clase por uid)
        try:
            from for3s_core.perfil import PerfilStore

            ps = PerfilStore(self._pool)
            out["perfil"] = await ps.get(uid)
            out["perfil_resumen"] = await ps.resumen(uid)
        except Exception:  # noqa: BLE001
            out["perfil"] = None
            out["perfil_resumen"] = None
        # tema activo + hilos (clase por uid)
        try:
            from for3s_core.temas import TemaStore

            ts = TemaStore(self._pool)
            out["tema_activo"] = await ts.activo(uid)
            out["hilos"] = await ts.resumen_hilos(uid, sesion_de(uid))
        except Exception:  # noqa: BLE001
            out["tema_activo"] = TEMA_GENERAL
            out["hilos"] = []
        # rol desde la tabla personas canónica (F1)
        try:
            async with self._pool.acquire() as con:
                r = await con.fetchrow(
                    "SELECT nombre, rol FROM personas WHERE telegram_user_id = $1", uid
                )
            out["nombre"] = r["nombre"] if r else None
            out["rol"] = r["rol"] if r else None
        except Exception:  # noqa: BLE001
            out["nombre"] = None
            out["rol"] = None
        return out

    def sesion(self, uid: int, tema: str | None = None) -> str:
        """Deriva la sesión canónica de una persona (delega en sesion_de)."""
        return sesion_de(uid, tema)
