# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — GOVERNOR del ecosistema de skills (H11 "APRENDE — el FRENO").

El governor es el CONTROL que debe existir ANTES del motor de auto-generación (H12).
Regla LOCKED (R6 §A, Grafo §8.4): un sistema que se auto-modifica necesita un freno
central que vea el ecosistema completo. Sin esto, H12 no arranca.

Diseño LOCKED (debate H11, 2026-06-25):
  • SCANNER de seguridad — el corazón. Muy conservador: ante cualquier patrón
    peligroso BLOQUEA la skill y deja constancia. (rm -rf, curl|sh, leer KEK/secrets,
    persistencia/cron, prompt-injection). Toda skill nueva pasa por aquí.
  • 3 FRENOS sobre datos reales (tabla `skills`, migración 019):
       FRENO 1 (generación/día): ≤ MAX_NEW_SKILLS_AUTO_PER_DAY auto-generadas al día.
       FRENO 4 (contradicción):  no duplicar (misma categoría+nombre ya activa).
       FRENO 5 (activas):        ≤ MAX_ACTIVE_SKILLS techo de complejidad.
  • 3 HOOKS honestos para H12 (requieren scoring/sandbox/NO-GO que aún no existen):
       FRENO 2 should_explore · FRENO 3 no_go_budget_ok · FRENO 6 independent_eval.
    Hoy devuelven un veredicto neutro y documentan qué los llenará. NO son frenos
    falsos: son puntos de extensión explícitos del R6 §A.6 ("envuelve, no reescribe").
  • KILL SWITCH: estado en BD (migración 020). Default auto-gen APAGADA. Solo el
    dueño lo cambia (/autogen on|off). + flag de entorno FOR3S_AUTOGEN_OFF como
    freno de emergencia adicional (si está, manda y apaga aunque la BD diga on).
  • PROVENANCE: el governor SOLO gestiona skills 'auto'. Las 'usuario' son intocables.

DEFENSIVO pero FAIL-CLOSED en lo que importa: si el scanner no puede decidir, BLOQUEA
(la seguridad no se salta por un error). El reporte de salud sí es best-effort.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("for3s.governor")

WORKSPACE_DEFAULT = "default"

# ── Calibración v1 (muy conservadora, R6: "calibración muy conservadora v1") ──
MAX_NEW_SKILLS_AUTO_PER_DAY = 3  # FRENO 1 — techo de auto-generación por día
MAX_ACTIVE_SKILLS = 100  # FRENO 5 — techo de complejidad del ecosistema

# Provenance (espejo de skills.py — el governor SOLO toca 'auto').
PROV_USUARIO = "usuario"
PROV_AUTO = "auto"

# Flag de entorno: freno de emergencia adicional al kill switch en BD.
ENV_AUTOGEN_OFF = "FOR3S_AUTOGEN_OFF"


# ───────────────────────── SCANNER de seguridad ─────────────────────────
# Patrones anti-peligro. Cada uno: (regex compilada, etiqueta legible).
# Muy conservador: preferimos un falso positivo (bloquear algo inocente) a dejar
# pasar una skill dañina. El dueño siempre puede crear a mano lo que el scanner
# rechace; lo que NO queremos es que la auto-generación (H12) cree algo peligroso.
_PATRONES_PELIGROSOS: list[tuple[re.Pattern, str]] = [
    # — Comandos destructivos —
    (
        re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", re.I),
        "borrado recursivo forzado (rm -rf)",
    ),
    (re.compile(r"\b(mkfs|dd\s+if=|shred|wipefs)\b", re.I), "formateo / destrucción de disco"),
    (re.compile(r":\(\)\s*\{.*\}\s*;\s*:|fork\s*bomb", re.I | re.S), "fork bomb"),
    (
        re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b|\bTRUNCATE\b", re.I),
        "DROP/TRUNCATE de base de datos",
    ),
    (
        re.compile(r"\b>\s*/dev/sd[a-z]|\bchmod\s+-R\s+777\s+/", re.I),
        "sobrescritura de dispositivo / permisos peligrosos en raíz",
    ),
    # — Descarga + ejecución (la vía clásica de malware) —
    (
        re.compile(r"\b(curl|wget|fetch)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b", re.I),
        "descarga directa ejecutada en shell (curl|sh)",
    ),
    (
        re.compile(r"\b(eval|exec)\s*\(\s*(base64|requests\.get|urllib|fetch)", re.I),
        "ejecución de código descargado dinámicamente",
    ),
    # — Exfiltración de secretos (CRÍTICO para For3s: KEK nunca sale) —
    (
        re.compile(r"\b(KEK|master[_-]?key|private[_-]?key|secret[_-]?store)\b", re.I),
        "acceso a material criptográfico / KEK",
    ),
    (
        re.compile(r"\b(\.env|id_rsa|\.ssh/|credentials|secrets?\.(json|ya?ml|txt))\b", re.I),
        "lectura de archivos de credenciales",
    ),
    (
        re.compile(r"\b(ANTHROPIC_TOKEN|TELEGRAM_BOT_TOKEN|DATABASE_URL|sk-ant-)", re.I),
        "exposición de tokens / credenciales de For3s",
    ),
    (
        re.compile(r"\b(os\.environ|getenv|printenv|env)\b[^\n]{0,40}\|[^\n]*(curl|nc|wget)", re.I),
        "exfiltración de variables de entorno por red",
    ),
    # — Persistencia / instalación silenciosa —
    (
        re.compile(
            r"\b(crontab|systemctl\s+enable|/etc/cron|@reboot|launchctl|"
            r"\.bashrc|\.profile|authorized_keys)\b",
            re.I,
        ),
        "intento de persistencia (cron / autostart / bashrc)",
    ),
    (
        re.compile(r"\b(nc|netcat|ncat)\b[^\n]*-[a-z]*e|/dev/tcp/", re.I),
        "shell reversa / bind shell",
    ),
    # — Prompt-injection (la skill intenta secuestrar al agente) —
    (
        re.compile(
            r"ignora(r)?\s+(todas?\s+)?(las\s+)?(instrucciones|reglas)\s+"
            r"(anteriores|previas|del\s+sistema)",
            re.I,
        ),
        "prompt-injection (anular instrucciones del sistema)",
    ),
    (
        re.compile(
            r"\b(ignore\s+(all\s+)?(previous|prior)\s+instructions|"
            r"disregard\s+(the\s+)?system\s+prompt|you\s+are\s+now\s+(a\s+)?DAN)\b",
            re.I,
        ),
        "prompt-injection (en inglés)",
    ),
    (
        re.compile(
            r"revela(r)?\s+(tu|el)\s+(system\s+prompt|prompt\s+del\s+sistema|"
            r"instrucciones\s+(secretas|ocultas))",
            re.I,
        ),
        "intento de extraer el system prompt",
    ),
]


@dataclass
class Veredicto:
    """Resultado de una verificación del governor."""

    permitido: bool
    freno: str = ""  # qué freno decidió (scanner|generacion|duplicado|activas|killswitch)
    motivo: str = ""  # explicación legible (para el dueño / auditoría)
    detalle: list[str] = field(default_factory=list)  # hallazgos del scanner

    def __bool__(self) -> bool:  # `if veredicto:` == permitido
        return self.permitido


def escanear(contenido: str, *, nombre: str = "", descripcion: str = "") -> Veredicto:
    """SCANNER (síncrono, sin BD): ¿el texto de la skill tiene patrones peligrosos?

    Revisa contenido + nombre + descripción. Muy conservador. Devuelve un Veredicto
    con TODOS los hallazgos (no se detiene en el primero, para reportar completo).

    FAIL-CLOSED: si algo falla al escanear, NO permite (la seguridad no se salta).
    """
    try:
        texto = "\n".join([nombre or "", descripcion or "", contenido or ""])
        hallazgos: list[str] = []
        for patron, etiqueta in _PATRONES_PELIGROSOS:
            if patron.search(texto):
                hallazgos.append(etiqueta)
        if hallazgos:
            return Veredicto(
                permitido=False,
                freno="scanner",
                motivo="El scanner de seguridad detectó patrón(es) peligroso(s).",
                detalle=hallazgos,
            )
        return Veredicto(permitido=True, freno="scanner", motivo="sin patrones peligrosos")
    except Exception:  # noqa: BLE001 — fail-closed: ante duda, bloquea
        logger.warning("scanner falló — bloqueo por seguridad (fail-closed)", exc_info=True)
        return Veredicto(
            permitido=False,
            freno="scanner",
            motivo="el scanner no pudo verificar (bloqueo por seguridad)",
        )


@dataclass(frozen=True)
class EcosystemHealth:
    """Foto de salud del ecosistema de skills (observabilidad del Pilar 3, R6 §A.5)."""

    workspace: str
    autogen_on: bool
    active_skills: int
    new_skills_auto_today: int
    bloqueos_today: int
    veredicto: str  # HEALTHY | THROTTLED | FROZEN


class SkillEcosystemGovernor:
    """Governance central del ecosistema de skills (Pilar 3, R6 §A).

    Gates síncronos (path crítico). El governor SOLO gestiona skills 'auto':
    las 'usuario' (las pidió un humano) son intocables.
    """

    def __init__(self, pool, *, workspace: str = WORKSPACE_DEFAULT) -> None:
        self._pool = pool
        self._ws = workspace

    # ───────────── KILL SWITCH ─────────────
    async def autogen_permitida(self) -> bool:
        """¿Está PERMITIDA la auto-generación? (kill switch). Default: NO.

        El flag de entorno FOR3S_AUTOGEN_OFF, si está, MANDA y apaga todo
        (freno de emergencia, no requiere BD ni Telegram)."""
        if os.environ.get(ENV_AUTOGEN_OFF, "").strip().lower() in ("1", "true", "yes", "on"):
            return False
        try:
            async with self._pool.acquire() as con:
                v = await con.fetchval(
                    "SELECT autogen_on FROM governor_estado WHERE workspace=$1", self._ws
                )
            return bool(v)
        except Exception:  # noqa: BLE001 — fail-closed: si no sé, está APAGADA
            logger.warning("no pude leer kill switch — asumo APAGADA", exc_info=True)
            return False

    async def set_autogen(self, on: bool, *, por: int | None = None, motivo: str = "") -> None:
        """Enciende/apaga la auto-generación (solo el dueño, vía /autogen). Persistido."""
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO governor_estado (workspace, autogen_on, cambiado_por, "
                " cambiado_at, motivo) VALUES ($1,$2,$3,now(),$4) "
                "ON CONFLICT (workspace) DO UPDATE SET autogen_on=$2, cambiado_por=$3, "
                " cambiado_at=now(), motivo=$4",
                self._ws,
                on,
                por,
                (motivo or "")[:200],
            )
        logger.info("[governor] kill switch autogen_on=%s por=%s", on, por)

    # ───────────── FRENO 1: generación/día ─────────────
    async def can_generate(self) -> Veredicto:
        """¿Se puede auto-generar otra skill HOY? (techo diario + kill switch)."""
        if not await self.autogen_permitida():
            return Veredicto(False, "killswitch", "La auto-generación está APAGADA (kill switch).")
        try:
            async with self._pool.acquire() as con:
                n = await con.fetchval(
                    "SELECT count(*) FROM skills WHERE provenance=$1 "
                    "AND creada_at >= date_trunc('day', now())",
                    PROV_AUTO,
                )
            if n >= MAX_NEW_SKILLS_AUTO_PER_DAY:
                return Veredicto(
                    False,
                    "generacion",
                    f"Techo diario de auto-generación alcanzado "
                    f"({n}/{MAX_NEW_SKILLS_AUTO_PER_DAY}). Se difiere a mañana.",
                )
            return Veredicto(True, "generacion", f"{n}/{MAX_NEW_SKILLS_AUTO_PER_DAY} hoy")
        except Exception:  # noqa: BLE001 — fail-closed
            return Veredicto(False, "generacion", "no pude verificar el techo diario")

    # ───────────── FRENO 4: contradicción (duplicado) ─────────────
    async def check_contradictions(self, nombre: str, categoria: str) -> Veredicto:
        """¿Choca con una skill ya activa? (HA-5, 2026-06-30 — endurecido)

        Antes: solo exact-match (categoría, nombre) → dejó pasar duplicados con nombres
        casi iguales ('pipeline-de-despliegue-de-botservicio' vs '...-bot-en-servidor').
        Ahora dos capas:
          1. exact-match (categoría + slug idéntico).
          2. SIMILITUD DE NOMBRE: misma categoría + el nombre comparte ≥70% de sus
             palabras significativas (Jaccard) con una skill activa → duplicado.
        (El embedding de skills existe ya — HA-5 — pero para el FRENO usamos Jaccard de
        nombre: no depende del modelo y es fail-closed limpio.) Fail-closed."""
        from for3s_core.skills import normalizar_nombre

        slug = normalizar_nombre(nombre)
        _STOP = {"de", "del", "la", "el", "en", "y", "a", "un", "una", "para", "con"}
        pal_nuevo = {p for p in slug.split("-") if len(p) >= 3 and p not in _STOP}
        try:
            async with self._pool.acquire() as con:
                existe = await con.fetchval(
                    "SELECT 1 FROM skills WHERE categoria=$1 AND nombre=$2 AND lifecycle='active'",
                    categoria,
                    slug,
                )
                if existe:
                    return Veredicto(
                        False,
                        "duplicado",
                        f"Ya existe una skill activa '{categoria}/{slug}'. "
                        f"Para cambiarla, edítala (no dupliques).",
                    )
                activas = await con.fetch(
                    "SELECT nombre FROM skills WHERE categoria=$1 AND lifecycle='active'",
                    categoria,
                )
            if pal_nuevo:
                for r in activas:
                    pal_exist = {
                        p for p in r["nombre"].split("-") if len(p) >= 3 and p not in _STOP
                    }
                    if not pal_exist:
                        continue
                    comunes = pal_nuevo & pal_exist
                    union = pal_nuevo | pal_exist
                    jaccard = len(comunes) / len(union) if union else 0.0
                    if jaccard >= 0.70:
                        return Veredicto(
                            False,
                            "duplicado",
                            f"Muy similar a la skill activa '{categoria}/{r['nombre']}' "
                            f"(solapa {len(comunes)}/{len(union)} palabras). "
                            f"Edítala en vez de duplicar.",
                        )
            return Veredicto(True, "duplicado", "sin colisión")
        except Exception:  # noqa: BLE001 — fail-closed
            return Veredicto(False, "duplicado", "no pude verificar duplicados")

    # ───────────── FRENO 5: techo de activas ─────────────
    async def active_budget_ok(self) -> Veredicto:
        """¿Cabe una skill más sin pasar el techo de complejidad del ecosistema?"""
        try:
            async with self._pool.acquire() as con:
                n = await con.fetchval("SELECT count(*) FROM skills WHERE lifecycle='active'")
            if n >= MAX_ACTIVE_SKILLS:
                return Veredicto(
                    False,
                    "activas",
                    f"Techo de skills activas alcanzado "
                    f"({n}/{MAX_ACTIVE_SKILLS}). Cura/archiva antes de crear más.",
                )
            return Veredicto(True, "activas", f"{n}/{MAX_ACTIVE_SKILLS} activas")
        except Exception:  # noqa: BLE001 — fail-closed
            return Veredicto(False, "activas", "no pude verificar el techo de activas")

    # ───────────── HOOKS para H12 (frenos 2/3/6, R6 §A.6) ─────────────
    # No son frenos falsos: son puntos de extensión explícitos. Hoy devuelven un
    # veredicto neutro porque la maquinaria que los activa (scoring dopaminérgico,
    # NO-GO rules, sandbox de skills) llega con H12. Documentados para no olvidarlos.

    async def should_explore(self) -> Veredicto:  # FRENO 2 — anti lock-in
        """HOOK H12: forzar exploración de skills alternativas (epsilon-greedy).
        Requiere scoring dopaminérgico (H12). Hoy: neutro."""
        return Veredicto(True, "exploracion", "hook H12 (requiere scoring)")

    async def no_go_budget_ok(self) -> Veredicto:  # FRENO 3 — salud NO-GO
        """HOOK H12: techo/expiración de reglas NO-GO. Requiere NO-GO rules (H12)."""
        return Veredicto(True, "no_go", "hook H12 (requiere NO-GO rules)")

    async def independent_eval(self, skill_nombre: str) -> Veredicto:  # FRENO 6
        """HOOK H12: evaluación independiente en sandbox (golden set + 2ª opinión),
        no juez-y-parte. Requiere sandbox de skills (H12). Hoy: neutro."""
        return Veredicto(True, "eval", "hook H12 (requiere sandbox de skills)")

    # ───────────── GATE de entrada: TODA skill nueva pasa por aquí ─────────────
    async def evaluar_skill_nueva(
        self,
        *,
        nombre: str,
        contenido: str,
        categoria: str = "general",
        descripcion: str = "",
        provenance: str = PROV_USUARIO,
        creada_por: int | None = None,
    ) -> Veredicto:
        """Puerta única que evalúa una skill ANTES de guardarla. Orden:

          1) SCANNER (siempre, usuario o auto) — el freno de daño. Si falla → BLOQUEA.
          2) Si provenance='auto' (la creó H12): además FRENO 1 (kill switch + día),
             FRENO 5 (activas) y FRENO 4 (duplicado).
          3) Si provenance='usuario' (la pidió un humano): solo scanner + duplicado.
             El dueño tiene autoridad; no le aplicamos techos de auto-generación.

        Cada bloqueo queda registrado (auditoría). Devuelve el primer Veredicto que
        niegue, o permitido=True si pasa todo."""
        # 1) SCANNER — siempre primero, para todos.
        v = escanear(contenido, nombre=nombre, descripcion=descripcion)
        if not v:
            await self._registrar_bloqueo(v, nombre, provenance, creada_por)
            return v

        # 2) Frenos de auto-generación (solo para 'auto'). Se evalúan en orden;
        #    en cuanto uno niega, se registra y se corta (no se crean corrutinas
        #    colgadas — cada chequeo se awaitea solo si se llega a él).
        if provenance == PROV_AUTO:
            v = await self.can_generate()
            if not v:
                await self._registrar_bloqueo(v, nombre, provenance, creada_por)
                return v
            v = await self.active_budget_ok()
            if not v:
                await self._registrar_bloqueo(v, nombre, provenance, creada_por)
                return v

        # 3) Duplicado (para todos — no queremos colisiones ni del usuario).
        v = await self.check_contradictions(nombre, categoria)
        if not v:
            await self._registrar_bloqueo(v, nombre, provenance, creada_por)
            return v

        return Veredicto(True, "", "skill aprobada por el governor")

    async def _registrar_bloqueo(
        self, v: Veredicto, nombre: str, provenance: str, creada_por: int | None
    ) -> None:
        """Deja constancia de un bloqueo (append-only, para auditoría + salud)."""
        try:
            motivo = v.motivo + ((" | " + "; ".join(v.detalle)) if v.detalle else "")
            async with self._pool.acquire() as con:
                await con.execute(
                    "INSERT INTO governor_bloqueos (workspace, freno, motivo, "
                    " skill_nombre, provenance, creada_por) VALUES ($1,$2,$3,$4,$5,$6)",
                    self._ws,
                    v.freno,
                    motivo[:500],
                    nombre[:64],
                    provenance,
                    creada_por,
                )
        except Exception:  # noqa: BLE001 — el registro nunca rompe la decisión
            logger.warning("no pude registrar bloqueo del governor", exc_info=True)

    # ───────────── Reporte de salud (observabilidad) ─────────────
    async def health_report(self) -> EcosystemHealth:
        """Foto del ecosistema (best-effort). Para /autogen status y el dueño."""
        autogen = await self.autogen_permitida()
        activas = nuevas = bloqueos = 0
        try:
            async with self._pool.acquire() as con:
                activas = (
                    await con.fetchval("SELECT count(*) FROM skills WHERE lifecycle='active'") or 0
                )
                nuevas = (
                    await con.fetchval(
                        "SELECT count(*) FROM skills WHERE provenance=$1 "
                        "AND creada_at >= date_trunc('day', now())",
                        PROV_AUTO,
                    )
                    or 0
                )
                bloqueos = (
                    await con.fetchval(
                        "SELECT count(*) FROM governor_bloqueos "
                        "WHERE creado_at >= date_trunc('day', now())"
                    )
                    or 0
                )
        except Exception:  # noqa: BLE001
            logger.warning("health_report best-effort falló parcialmente", exc_info=True)
        if not autogen:
            verdict = "FROZEN"
        elif nuevas >= MAX_NEW_SKILLS_AUTO_PER_DAY or activas >= MAX_ACTIVE_SKILLS:
            verdict = "THROTTLED"
        else:
            verdict = "HEALTHY"
        return EcosystemHealth(self._ws, autogen, activas, nuevas, bloqueos, verdict)
