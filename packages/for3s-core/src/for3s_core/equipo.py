# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — EQUIPO multi-usuario (H8 S10, 2026-06-23).

Convierte a For3s de single-owner (1 persona) a multi-usuario: varias personas
comparten un mismo agente, con roles (encargado/miembro) y un control de acceso
tipo PUERTA.

MODELO "PUERTA" (decisión de diseño — gran UX, sin pedir user_ids):
  · 🟢 abierta  → quien le escriba al bot ENTRA al equipo y queda registrado.
  · 🔴 cerrada  → nadie nuevo entra; solo pasan el dueño + miembros registrados.
Default cerrada (fail-closed). Sacar/denegar miembros se diseña MÁS ADELANTE.

ADITIVO Y SEGURO: si no hay equipo configurado, el bot sigue operando single-owner
exactamente como hoy (el dueño manda en OwnerStore y aquí también pasa siempre).
Persistencia en PostgreSQL (tablas equipos / equipo_miembros, migración 010).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("for3s.equipo")

ROL_ENCARGADO = "encargado"
ROL_MIEMBRO = "miembro"
ROLES = (ROL_ENCARGADO, ROL_MIEMBRO)


# ============================================================================
# S10b — ROLES + PERMISOS (decisión de diseño: encargado admin, miembro propone).
# ============================================================================
# Acciones que el bot sabe controlar. Lógica PURA (sin BD) → fácil de razonar
# y testear. El gate real de aprobación (botones) lo cablea S10d usando esto.
#
# Tres niveles por acción:
#   "si"      → la persona puede hacerla directo.
#   "propone" → puede pedirla, pero el ENCARGADO debe aprobarla (gate S10d).
#   "no"      → no puede.
ACCION_CONVERSAR = "conversar"  # chatear, preguntar, usar memoria
ACCION_LANZAR_EQUIPO = "lanzar_equipo"  # disparar el multi-agente (análisis)
ACCION_SENSIBLE = "accion_sensible"  # escribir en GitHub, borrar, etc.
ACCION_PUERTA = "puerta"  # abrir/cerrar /invitar
ACCION_GESTION = "gestion_miembros"  # gestionar miembros (kick a futuro)

# matriz rol → acción → nivel
_PERMISOS: dict[str, dict[str, str]] = {
    ROL_ENCARGADO: {
        ACCION_CONVERSAR: "si",
        ACCION_LANZAR_EQUIPO: "si",
        ACCION_SENSIBLE: "si",  # el encargado ejecuta directo
        ACCION_PUERTA: "si",
        ACCION_GESTION: "si",
    },
    ROL_MIEMBRO: {
        ACCION_CONVERSAR: "si",
        ACCION_LANZAR_EQUIPO: "si",
        ACCION_SENSIBLE: "propone",  # propone → encargado aprueba
        ACCION_PUERTA: "no",
        ACCION_GESTION: "no",
    },
}


def nivel_permiso(rol: str | None, accion: str) -> str:
    """Nivel de permiso de un rol para una acción: 'si' | 'propone' | 'no'.
    Rol desconocido o None → 'no' (fail-closed)."""
    return _PERMISOS.get(rol or "", {}).get(accion, "no")


def puede(rol: str | None, accion: str) -> bool:
    """¿Puede el rol hacer la acción directo (sin aprobación)? Solo 'si' es True."""
    return nivel_permiso(rol, accion) == "si"


def requiere_aprobacion(rol: str | None, accion: str) -> bool:
    """¿La acción necesita que el encargado la apruebe para este rol? ('propone')."""
    return nivel_permiso(rol, accion) == "propone"


@dataclass(frozen=True)
class Miembro:
    """Una persona dentro del equipo."""

    user_id: int
    rol: str
    nombre: str | None = None
    ultima_actividad: object | None = None  # M3: timestamp del último turno (health)


@dataclass(frozen=True)
class Solicitud:
    """Una acción sensible PROPUESTA por un miembro, pendiente de aprobación (S10d)."""

    id: int
    equipo_id: int
    solicitante_id: int
    accion: str
    descripcion: str
    payload: dict
    estado: str  # 'pendiente' | 'aprobada' | 'rechazada'
    resuelta_por: int | None = None


def _row_a_solicitud(r) -> Solicitud:
    """Convierte una fila de la tabla solicitudes en Solicitud (parsea el payload)."""
    import json

    payload = r["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return Solicitud(
        id=r["id"],
        equipo_id=r["equipo_id"],
        solicitante_id=r["solicitante_id"],
        accion=r["accion"],
        descripcion=r["descripcion"],
        payload=payload or {},
        estado=r["estado"],
        resuelta_por=r["resuelta_por"],
    )


class EquipoStore:
    """Store del equipo sobre PostgreSQL. Maneja el equipo del dueño, sus miembros
    y el estado de la puerta. Async, sin ORM (asyncpg), igual que el resto del core."""

    def __init__(self, pool) -> None:
        self._pool = pool

    # ---- ciclo de vida del equipo -------------------------------------------------

    async def asegurar_equipo(
        self,
        encargado_id: int,
        nombre: str = "Mi equipo",
        *,
        nombre_encargado: str | None = None,
    ) -> int:
        """Garantiza que el dueño tenga su equipo (idempotente). Lo crea con la puerta
        CERRADA y registra al dueño como encargado. Devuelve el equipo_id.

        nombre_encargado (M1): nombre legible del encargado para mostrar en /miembros
        (antes salía '(sin nombre)'). Si el equipo YA existe pero el encargado no tiene
        nombre guardado, lo rellena aquí (auto-cura registros viejos en el próximo uso).

        Se llama cuando el dueño usa una función de equipo (ej. /invitar) por primera
        vez — NO al arranque, para no romper el modo single-owner de quien no use equipo."""
        async with self._pool.acquire() as con:
            eid = await con.fetchval(
                "SELECT id FROM equipos WHERE encargado_id = $1",
                encargado_id,
            )
            if eid is not None:
                # M1: auto-curar el nombre del encargado si está vacío y ahora lo tenemos
                if nombre_encargado:
                    await con.execute(
                        "UPDATE equipo_miembros SET nombre = $3 "
                        "WHERE equipo_id = $1 AND user_id = $2 AND (nombre IS NULL OR nombre = '')",
                        eid,
                        encargado_id,
                        nombre_encargado,
                    )
                return eid
            eid = await con.fetchval(
                "INSERT INTO equipos (nombre, encargado_id, puerta_abierta) "
                "VALUES ($1, $2, false) RETURNING id",
                nombre,
                encargado_id,
            )
            await con.execute(
                "INSERT INTO equipo_miembros (equipo_id, user_id, rol, nombre) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
                eid,
                encargado_id,
                ROL_ENCARGADO,
                nombre_encargado,
            )
            logger.info("[equipo] creado equipo=%s encargado=%s", eid, encargado_id)
            return eid

    async def equipo_de(self, user_id: int) -> int | None:
        """Devuelve el equipo_id donde esta persona es MIEMBRO activo, o None."""
        async with self._pool.acquire() as con:
            return await con.fetchval(
                "SELECT equipo_id FROM equipo_miembros "
                "WHERE user_id = $1 AND activo ORDER BY entro_at LIMIT 1",
                user_id,
            )

    # ---- la PUERTA ---------------------------------------------------------------

    async def puerta_abierta(self, equipo_id: int) -> bool:
        async with self._pool.acquire() as con:
            return bool(
                await con.fetchval(
                    "SELECT puerta_abierta FROM equipos WHERE id = $1",
                    equipo_id,
                )
            )

    async def set_puerta(self, equipo_id: int, abierta: bool) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE equipos SET puerta_abierta = $2 WHERE id = $1",
                equipo_id,
                abierta,
            )
        logger.info(
            "[equipo] puerta equipo=%s -> %s", equipo_id, "ABIERTA" if abierta else "CERRADA"
        )

    # ---- miembros ----------------------------------------------------------------

    async def agregar_miembro(
        self,
        equipo_id: int,
        user_id: int,
        *,
        nombre: str | None = None,
        rol: str = ROL_MIEMBRO,
        reinvitar: bool = False,
    ) -> bool:
        """Registra (o re-activa) a una persona como miembro. Devuelve True si ENTRÓ
        nueva (o re-activada), False si ya estaba activa o está EXPULSADA y no es una
        re-invitación. Idempotente.

        reinvitar (C-v): si True, es el ENCARGADO re-admitiendo explícitamente →
        limpia la bandera expulsado. Si False (entrada por la puerta), un EXPULSADO
        NO re-entra (sacar = denegar de verdad)."""
        async with self._pool.acquire() as con:
            estado = await con.fetchrow(
                "SELECT rol, activo, expulsado FROM equipo_miembros "
                "WHERE equipo_id = $1 AND user_id = $2",
                equipo_id,
                user_id,
            )
            if estado is not None and estado["activo"]:
                return False  # ya estaba dentro
            # C-v: un expulsado NO re-entra por la puerta (solo con re-invitación)
            if estado is not None and estado["expulsado"] and not reinvitar:
                return False
            await con.execute(
                "INSERT INTO equipo_miembros (equipo_id, user_id, rol, nombre, activo, expulsado) "
                "VALUES ($1, $2, $3, $4, true, false) "
                "ON CONFLICT (equipo_id, user_id) DO UPDATE "
                "SET activo = true, expulsado = false, "
                "    nombre = COALESCE($4, equipo_miembros.nombre)",
                equipo_id,
                user_id,
                rol,
                nombre,
            )
            # F1 REDISEÑO MEMORIA (2026-07-01): mantener la tabla PERSONAS canónica
            # sincronizada. Sin esto quedaría obsoleta al entrar alguien nuevo (bug de
            # desincronización). Defensivo: si personas no existe (BD sin migr 026),
            # no rompe el alta del miembro.
            try:
                await con.execute(
                    "INSERT INTO personas (telegram_user_id, nombre, rol) "
                    "VALUES ($1, $2, $3) "
                    "ON CONFLICT (telegram_user_id) DO UPDATE "
                    "SET rol = EXCLUDED.rol, "
                    "    nombre = COALESCE(personas.nombre, EXCLUDED.nombre), "
                    "    actualizada_at = now()",
                    user_id,
                    nombre,
                    rol,
                )
            except Exception:  # noqa: BLE001 — personas es aditiva, no debe romper el alta
                logger.warning("[equipo] no pude sincronizar personas para user=%s", user_id)
            logger.info(
                "[equipo] miembro entró equipo=%s user=%s rol=%s reinvitar=%s",
                equipo_id,
                user_id,
                rol,
                reinvitar,
            )
            return True

    async def sacar_miembro(
        self, equipo_id: int, encargado_id: int, objetivo_id: int
    ) -> tuple[bool, str]:
        """C-v: el ENCARGADO saca a un miembro (soft-remove: activo=false +
        expulsado=true → pierde acceso, su historial NO se borra, y NO re-entra por
        la puerta abierta). Verificación en BD (no confiar en el caller): quien saca
        debe ser ENCARGADO de ese equipo; NO se puede sacar a sí mismo ni a otro
        encargado. Devuelve (ok, motivo)."""
        if objetivo_id == encargado_id:
            return False, "no_puedes_sacarte"
        async with self._pool.acquire() as con:
            async with con.transaction():
                rol_enc = await con.fetchval(
                    "SELECT rol FROM equipo_miembros WHERE equipo_id=$1 AND user_id=$2 AND activo",
                    equipo_id,
                    encargado_id,
                )
                if rol_enc != ROL_ENCARGADO:
                    return False, "no_eres_encargado"
                obj = await con.fetchrow(
                    "SELECT rol, activo FROM equipo_miembros WHERE equipo_id=$1 AND user_id=$2",
                    equipo_id,
                    objetivo_id,
                )
                if obj is None or not obj["activo"]:
                    return False, "no_es_miembro"
                if obj["rol"] == ROL_ENCARGADO:
                    return False, "no_puedes_sacar_encargado"
                await con.execute(
                    "UPDATE equipo_miembros SET activo=false, expulsado=true "
                    "WHERE equipo_id=$1 AND user_id=$2",
                    equipo_id,
                    objetivo_id,
                )
        logger.info(
            "[equipo] encargado=%s sacó a user=%s (equipo=%s)", encargado_id, objetivo_id, equipo_id
        )
        return True, "ok"

    async def miembros(self, equipo_id: int) -> list[Miembro]:
        """Miembros activos del equipo + su ÚLTIMA ACTIVIDAD (M3, health real): se
        cruza con episodes_events por telegram_user_id. Defensivo: si el cruce falla,
        devuelve los miembros sin actividad (no rompe /miembros)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT m.user_id, m.rol, m.nombre, "
                "       (SELECT max(e.created_at) FROM episodes_events e "
                "        WHERE e.telegram_user_id = m.user_id AND e.deleted_at IS NULL) "
                "       AS ultima "
                "FROM equipo_miembros m "
                "WHERE m.equipo_id = $1 AND m.activo ORDER BY m.entro_at",
                equipo_id,
            )
        return [Miembro(r["user_id"], r["rol"], r["nombre"], r["ultima"]) for r in rows]

    async def rol_de(self, equipo_id: int, user_id: int) -> str | None:
        async with self._pool.acquire() as con:
            return await con.fetchval(
                "SELECT rol FROM equipo_miembros WHERE equipo_id = $1 AND user_id = $2 AND activo",
                equipo_id,
                user_id,
            )

    # ---- autorización (el corazón, ADITIVO a OwnerStore) -------------------------

    async def autorizar(
        self, owner_id: int | None, user_id: int | None, *, nombre: str | None = None
    ) -> tuple[bool, str]:
        """¿Puede esta persona usar el bot? Lógica ADITIVA y FAIL-CLOSED:

          1. Sin user_id → denegado.
          2. Es el dueño (owner_id) → SIEMPRE pasa (compat single-owner; el dueño
             es el encargado del equipo).
          3. Ya es miembro activo de algún equipo → pasa.
          4. Hay equipo del dueño con la PUERTA ABIERTA → entra (se registra como
             miembro) y pasa.
          5. En cualquier otro caso → denegado.

        Devuelve (autorizado, motivo). El motivo sirve para logs y para el mensaje
        al usuario (ej. puerta cerrada)."""
        if user_id is None:
            return False, "sin_user_id"

        if owner_id is not None and user_id == owner_id:
            return True, "dueño"

        # ¿ya es miembro de algún equipo?
        if await self.equipo_de(user_id) is not None:
            return True, "miembro"

        # ¿el equipo del dueño tiene la puerta abierta? → entra (salvo EXPULSADO)
        if owner_id is not None:
            async with self._pool.acquire() as con:
                eid = await con.fetchval(
                    "SELECT id FROM equipos WHERE encargado_id = $1 AND puerta_abierta",
                    owner_id,
                )
                expulsado = False
                if eid is not None:
                    expulsado = bool(
                        await con.fetchval(
                            "SELECT expulsado FROM equipo_miembros "
                            "WHERE equipo_id=$1 AND user_id=$2",
                            eid,
                            user_id,
                        )
                    )
            if eid is not None and not expulsado:
                # agregar_miembro devuelve False si está expulsado (no re-entra)
                entro = await self.agregar_miembro(eid, user_id, nombre=nombre)
                if entro:
                    return True, "puerta_abierta"
            if expulsado:
                return False, "expulsado"  # C-v: sacado = denegado, no re-entra

        return False, "puerta_cerrada"

    # ---- S10d: gate de aprobación del encargado ---------------------------------

    async def crear_solicitud(
        self,
        equipo_id: int,
        solicitante_id: int,
        accion: str,
        descripcion: str,
        *,
        payload: dict | None = None,
    ) -> int:
        """Un miembro PROPONE una acción sensible. Crea una solicitud 'pendiente'
        y devuelve su id (para avisar al encargado con botones aprobar/rechazar)."""
        import json

        async with self._pool.acquire() as con:
            sid = await con.fetchval(
                "INSERT INTO solicitudes "
                "(equipo_id, solicitante_id, accion, descripcion, payload) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                equipo_id,
                solicitante_id,
                accion,
                descripcion,
                json.dumps(payload or {}),
            )
        logger.info(
            "[equipo] solicitud=%s creada equipo=%s por=%s accion=%s",
            sid,
            equipo_id,
            solicitante_id,
            accion,
        )
        return sid

    async def get_solicitud(self, solicitud_id: int) -> Solicitud | None:
        async with self._pool.acquire() as con:
            r = await con.fetchrow(
                "SELECT id, equipo_id, solicitante_id, accion, descripcion, "
                "payload, estado, resuelta_por FROM solicitudes WHERE id = $1",
                solicitud_id,
            )
        return _row_a_solicitud(r) if r else None

    async def pendientes_de(self, equipo_id: int) -> list[Solicitud]:
        """Solicitudes pendientes de un equipo (las que el encargado debe revisar)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, equipo_id, solicitante_id, accion, descripcion, "
                "payload, estado, resuelta_por FROM solicitudes "
                "WHERE equipo_id = $1 AND estado = 'pendiente' ORDER BY creada_at",
                equipo_id,
            )
        return [_row_a_solicitud(r) for r in rows]

    async def _resolver(
        self,
        solicitud_id: int,
        encargado_id: int,
        nuevo_estado: str,
    ) -> Solicitud | None:
        """Aprueba o rechaza, SOLO si: existe, sigue pendiente y quien resuelve es
        ENCARGADO de ESE equipo (autorización en BD, no confiar en el caller).
        Devuelve la solicitud actualizada, o None si no procede (fail-closed)."""
        async with self._pool.acquire() as con:
            async with con.transaction():
                r = await con.fetchrow(
                    "SELECT s.*, m.rol AS rol_resolutor FROM solicitudes s "
                    "LEFT JOIN equipo_miembros m ON m.equipo_id = s.equipo_id "
                    "AND m.user_id = $2 AND m.activo "
                    "WHERE s.id = $1 FOR UPDATE OF s",
                    solicitud_id,
                    encargado_id,
                )
                if r is None or r["estado"] != "pendiente":
                    return None
                if r["rol_resolutor"] != ROL_ENCARGADO:
                    logger.warning(
                        "[equipo] solicitud=%s: %s NO es encargado, rechazo",
                        solicitud_id,
                        encargado_id,
                    )
                    return None
                await con.execute(
                    "UPDATE solicitudes SET estado = $2, resuelta_por = $3, "
                    "resuelta_at = now() WHERE id = $1",
                    solicitud_id,
                    nuevo_estado,
                    encargado_id,
                )
        logger.info(
            "[equipo] solicitud=%s -> %s por encargado=%s", solicitud_id, nuevo_estado, encargado_id
        )
        return await self.get_solicitud(solicitud_id)

    async def aprobar(self, solicitud_id: int, encargado_id: int) -> Solicitud | None:
        """El encargado APRUEBA. Devuelve la solicitud (con payload, para ejecutarla)
        o None si no procede."""
        return await self._resolver(solicitud_id, encargado_id, "aprobada")

    async def rechazar(self, solicitud_id: int, encargado_id: int) -> Solicitud | None:
        """El encargado RECHAZA. Devuelve la solicitud o None si no procede."""
        return await self._resolver(solicitud_id, encargado_id, "rechazada")
