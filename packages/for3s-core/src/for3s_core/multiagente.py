"""For3s OS — red multi-agente (H8 "EQUIPO"). Hogar del Hub, el message bus y el
Synthesizer que coordinan el equipo de specialists.

Construcción por sub-pasos (cada uno explicado + verificado, ver H8_Plan_Maestro):
  · S3 (este): MessageBus — el "sistema de correo interno" del equipo (asyncio.Queue).
  · S4: Hub orquestador (decide familia + spawn specialists con cola anti-429).
  · S5: Synthesizer (combina los N reportes en 1).
  · S6+: capas de no-bloqueo, memoria, cost control, aislamiento, multi-usuario.

Diseño LOCKED: R5 B3 §5.3.3. asyncio.Queue porque los specialists corren en el MISMO
proceso (asyncio.create_task), no son servidores → sin Kafka/red, instantáneo, con
límite de tamaño (backpressure) para no desbordar memoria.
"""

from __future__ import annotations

import asyncio
import logging
import resource
from dataclasses import dataclass

logger = logging.getLogger("for3s.multiagente")


@dataclass
class Mensaje:
    """Un mensaje en el bus. Simple y explícito."""

    de: str               # quién lo manda (hub | nombre del specialist)
    para: str             # destino (hub | nombre del specialist | "all")
    tipo: str             # tarea | reporte | evento
    contenido: object = None


class MessageBus:
    """El correo interno del equipo (R5 B3 §5.3.3). UN bus por "batch" (una corrida
    del equipo sobre una tarea). Buzón central del Hub + un buzón por specialist +
    broadcast a todos. Colas con maxsize → si se llenan, hay backpressure (no explota
    la RAM; anticipo de las capas de memoria de S7)."""

    HUB_INBOX_MAXSIZE = 1000
    SPECIALIST_INBOX_MAXSIZE = 100

    def __init__(self, specialists: list[str]) -> None:
        # buzón central: todos los specialists dejan aquí sus reportes
        self.hub_inbox: asyncio.Queue = asyncio.Queue(maxsize=self.HUB_INBOX_MAXSIZE)
        # un buzón por specialist: el Hub les deja aquí su tarea/instrucciones
        self.specialist_inbox: dict[str, asyncio.Queue] = {
            name: asyncio.Queue(maxsize=self.SPECIALIST_INBOX_MAXSIZE)
            for name in specialists
        }
        # broadcast HUB → ALL (ej. "cancelen"): evento + payload
        self.broadcast_event: asyncio.Event = asyncio.Event()
        self.broadcast_payload: object = None

    # --- Hub → specialist ---------------------------------------------------
    async def enviar_a_specialist(self, nombre: str, msg: Mensaje) -> bool:
        """El Hub deja un mensaje en el buzón de un specialist. False si no existe
        o el buzón está lleno (backpressure → no bloquea indefinido)."""
        cola = self.specialist_inbox.get(nombre)
        if cola is None:
            return False
        try:
            cola.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            logger.warning("[bus] inbox de %s lleno → backpressure", nombre)
            return False

    async def recibir_specialist(self, nombre: str) -> Mensaje:
        """Un specialist espera su próximo mensaje (bloquea hasta que llegue)."""
        return await self.specialist_inbox[nombre].get()

    # --- specialist → Hub ---------------------------------------------------
    async def reportar_al_hub(self, msg: Mensaje) -> bool:
        """Un specialist deja su reporte en el buzón central del Hub."""
        try:
            self.hub_inbox.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            logger.warning("[bus] hub_inbox lleno → backpressure")
            return False

    async def recibir_del_hub_inbox(self) -> Mensaje:
        """El Hub espera el próximo reporte de cualquier specialist."""
        return await self.hub_inbox.get()

    # --- Hub → ALL (broadcast) ---------------------------------------------
    def broadcast(self, payload: object) -> None:
        """El Hub avisa a TODOS a la vez (ej. cancelar). Los specialists chequean
        broadcast_event entre pasos para reaccionar."""
        self.broadcast_payload = payload
        self.broadcast_event.set()

    def hay_broadcast(self) -> bool:
        """True si hay un broadcast activo (un specialist lo consulta para abortar)."""
        return self.broadcast_event.is_set()


# ============================================================================
# S4 — HUB orquestador: decide familia + spawn paralelo (gobernado) + recoge
# ============================================================================

# Máximo de specialists corriendo A LA VEZ (semáforo anti-429). Conservador
# (2026-06-23): 2 simultáneos — más rápido que secuencial, sin topar el
# rate instantáneo del OAuth. Subir si se confirma que aguanta. Cost control = S8.
CONCURRENCIA_MAX = 2
PAUSA_ENTRE_LANZAMIENTOS_SEG = 1.0  # pequeño respiro entre spawns

# S6 — TIMEOUT GLOBAL del equipo: tope de tiempo para TODA la corrida. Si el equipo
# entero excede esto, se corta y se entrega lo que SÍ llegó (no se cuelga el bot por
# un specialist atascado). Cada specialist ya tiene su timeout_seg propio (S2); este
# es la red por si varios se acumulan o uno escapa de su timeout individual.
TIMEOUT_EQUIPO_SEG = 180


@dataclass
class ResultadoEquipo:
    """Lo que el Hub devuelve tras coordinar al equipo (para el Synthesizer, S5)."""

    familia: str
    reportes: list          # list[ResultadoSpecialist]
    segundos_total: float
    n_ok: int               # cuántos specialists completaron con éxito
    costo: object = None     # PresupuestoCorrida (cost control S8) — tokens/llamadas


def decidir_familia(tarea: str) -> str:
    """Elige la familia de specialists según la tarea (§3.4). Si huele a GitHub/
    código → 'tecnica'; si no → 'general'. Reusa el detector ya existente."""
    try:
        from for3s_core.conversation import huele_a_github
        return "tecnica" if huele_a_github(tarea) else "general"
    except Exception:  # noqa: BLE001 — si falla el detector, default general (más amplio)
        return "general"


async def correr_equipo(
    tarea: str, *, provider=None, familia: str | None = None, on_progreso=None,
) -> ResultadoEquipo:
    """El HUB coordina al equipo sobre `tarea` (S4):
      1. decide la FAMILIA (técnica/general) — o usa la forzada.
      2. lanza sus specialists EN PARALELO pero GOBERNADO (semáforo CONCURRENCIA_MAX
         + pausa entre lanzamientos) → más rápido que secuencial sin topar el 429.
      3. recoge todos los reportes (defensivo: un specialist caído no tumba al equipo).
    Devuelve ResultadoEquipo (los reportes van al Synthesizer en S5).

    on_progreso (PULIDO H8 área A): callback OPCIONAL `async on_progreso(evento)` que
    se llama cuando (a) arranca el equipo: evento={"tipo":"inicio","nombres":[...]};
    (b) un specialist EMPIEZA a trabajar: evento={"tipo":"trabajando","nombre":str};
    (c) un specialist TERMINA: evento={"tipo":"fin","nombre":str,"ok":bool}. Sirve para
    pintar progreso EN VIVO en Telegram. ADITIVO: si es None, igual que antes.
    DEFENSIVO: si el callback falla, NO rompe la corrida del equipo.
    """
    import time

    from for3s_core import specialists as sp

    async def _avisar(evento):
        if on_progreso is None:
            return
        try:
            await on_progreso(evento)
        except Exception:  # noqa: BLE001 — el progreso es cosmético, nunca rompe
            logger.warning("[hub] callback de progreso falló (ignoro)")

    t0 = time.time()
    fam = familia or decidir_familia(tarea)
    defs = sp.de_familia(fam)
    logger.info("[hub] tarea → familia '%s' (%d specialists)", fam, len(defs))

    # COST CONTROL S8 — capa 1 (pre-flight): ¿hay presupuesto para esta corrida?
    from for3s_core.cost_control import PresupuestoCorrida
    presupuesto = PresupuestoCorrida()
    puede, razon = presupuesto.puede_lanzar(len(defs))
    if not puede:
        # recortar a lo que el budget permite (capa 1: no lanzar de más)
        cupo = max(1, int(presupuesto.max_llamadas / 1.3) - presupuesto.llamadas)
        logger.warning("[hub] budget: %s → recorto a %d specialists", razon, cupo)
        defs = defs[:cupo]

    # provider único reutilizado por todos (un solo bucket de rate-limit)
    if provider is None:
        from for3s_core.config import load_settings
        from for3s_core.llm import ClaudeProvider
        s = load_settings()
        provider = ClaudeProvider(token=s.anthropic_token, oauth=s.is_oauth, model=s.model)

    # avisar el INICIO con la lista de specialists (para pintar el progreso vacío)
    await _avisar({"tipo": "inicio", "nombres": [d.nombre for d in defs]})

    # semáforo: máximo CONCURRENCIA_MAX specialists a la vez (anti-429 instantáneo)
    sem = asyncio.Semaphore(CONCURRENCIA_MAX)

    async def _uno(definicion):
        async with sem:
            # avisar que ESTE arranca (ya pasó el semáforo) → pintar 🔄 "en curso"
            await _avisar({"tipo": "trabajando", "nombre": definicion.nombre})
            r = await sp.correr_specialist(definicion, tarea, provider=provider)
        # avisar que ESTE terminó (para pintar 🟢/🔴 en vivo)
        await _avisar({"tipo": "fin", "nombre": definicion.nombre, "ok": r.ok})
        return r

    # lanzar todos como tasks (el semáforo serializa a CONCURRENCIA_MAX reales)
    tasks = []
    for i, d in enumerate(defs):
        if i > 0:
            await asyncio.sleep(PAUSA_ENTRE_LANZAMIENTOS_SEG)
        tasks.append(asyncio.create_task(_uno(d)))

    # S6 NO-BLOQUEO: timeout GLOBAL. Si el equipo entero excede TIMEOUT_EQUIPO_SEG,
    # cortamos: cancelamos lo pendiente y armamos reportes "no completó" para esos.
    # Así un specialist atascado NUNCA cuelga al bot — se entrega lo que sí llegó.
    from for3s_core.specialists import ResultadoSpecialist
    try:
        reportes = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=TIMEOUT_EQUIPO_SEG,
        )
    except TimeoutError:
        logger.warning("[hub] TIMEOUT GLOBAL del equipo (%ds) → corto y entrego lo que hay",
                       TIMEOUT_EQUIPO_SEG)
        reportes = []
        for d, t in zip(defs, tasks, strict=True):
            if t.done() and not t.cancelled() and t.exception() is None:
                reportes.append(t.result())
            else:
                t.cancel()
                reportes.append(ResultadoSpecialist(
                    nombre=d.nombre, ok=False,
                    texto=f"({d.nombre} no completó: timeout global del equipo)"))

    # normalizar: una excepción del gather → reporte fallido (defensa extra)
    norm = []
    for d, r in zip(defs, reportes, strict=True):
        if isinstance(r, ResultadoSpecialist):
            norm.append(r)
        else:  # excepción escapada → no tumba al equipo
            logger.warning("[hub] %s lanzó %s → reporte fallido", d.nombre, type(r).__name__)
            norm.append(ResultadoSpecialist(
                nombre=d.nombre, ok=False, texto=f"({d.nombre} falló: {type(r).__name__})"))
    reportes = norm

    # COST CONTROL S8 — capa 3 (monitoring): sumar el gasto de cada specialist al
    # presupuesto de la corrida (tokens + llamadas). Queda para el reporte (capa 6).
    for r in reportes:
        if r.ok:
            presupuesto.registrar(r.tokens_in, r.tokens_out)

    dt = time.time() - t0
    n_ok = sum(1 for r in reportes if r.ok)
    logger.info("[hub] equipo '%s' terminó: %d/%d ok en %.1fs · %s",
                fam, n_ok, len(reportes), dt, presupuesto.reporte())
    return ResultadoEquipo(
        familia=fam, reportes=reportes, segundos_total=dt, n_ok=n_ok, costo=presupuesto,
    )


# ============================================================================
# S5 — SYNTHESIZER: combina los N reportes en 1 informe unificado
# ============================================================================

# Etiquetas legibles por specialist (para el informe y el prompt de síntesis).
_ETIQUETAS = {
    "code_analyzer": "Análisis de código",
    "security_auditor": "Seguridad",
    "test_generator": "Tests/cobertura",
    "performance_analyzer": "Rendimiento",
    "doc_writer": "Documentación",
    "investigador": "Investigación",
    "escritor": "Redacción",
    "analista": "Análisis",
    "planificador": "Plan",
    "critico": "Crítica/riesgos",
}


def _bloques_crudos(equipo: ResultadoEquipo) -> str:
    """Concatena los reportes OK con su etiqueta (fallback si la síntesis LLM falla,
    y material que se le pasa al synthesizer)."""
    partes = []
    for r in equipo.reportes:
        if r.ok:
            etq = _ETIQUETAS.get(r.nombre, r.nombre)
            partes.append(f"### [{etq}]\n{r.texto.strip()}")
    return "\n\n".join(partes)


_INSTRUCCION_SINTESIS = (
    "Eres el EDITOR JEFE de un equipo de análisis. Abajo están los informes de varios "
    "especialistas que analizaron lo MISMO desde su ángulo. Combínalos en UN solo informe "
    "claro y accionable, en español:\n"
    "1. Empieza con un RESUMEN EJECUTIVO de 2-3 líneas (el veredicto general).\n"
    "2. Luego los hallazgos ORDENADOS POR PRIORIDAD (lo crítico primero, con su severidad).\n"
    "3. NO repitas: si dos especialistas dicen lo mismo, fusiónalo y di que coinciden.\n"
    "4. Mantén lo concreto (ejemplos, fixes) pero quita el relleno.\n"
    "5. Cierra con los PRÓXIMOS PASOS sugeridos.\n"
    "NO inventes hallazgos que los especialistas no reportaron. Sé fiel a sus informes.\n\n"
    "=== INFORMES DE LOS ESPECIALISTAS ===\n"
)


async def sintetizar(equipo: ResultadoEquipo, *, provider=None) -> str:
    """Combina los reportes del equipo (S4) en UN informe unificado (S5).

    Filtra los specialists que fallaron (solo combina los OK) y, si alguno cayó, lo
    MENCIONA honesto al final (no finge tener su análisis). OAUTH-SAFE: instrucción en
    el user message, system="". DEFENSIVA: si la síntesis LLM falla, devuelve los
    bloques crudos (mejor eso que nada).
    """
    crudos = _bloques_crudos(equipo)
    caidos = [r.nombre for r in equipo.reportes if not r.ok]
    nota_caidos = ""
    if caidos:
        etqs = ", ".join(_ETIQUETAS.get(n, n) for n in caidos)
        nota_caidos = f"\n\n⚠️ No se completó el análisis de: {etqs} (puedes pedirlo de nuevo)."

    if not crudos:
        return (
            "Ningún especialista pudo completar su análisis. Reintenta en un momento."
            + nota_caidos
        )

    try:
        if provider is None:
            from for3s_core.config import load_settings
            from for3s_core.llm import ClaudeProvider
            s = load_settings()
            provider = ClaudeProvider(token=s.anthropic_token, oauth=s.is_oauth, model=s.model)
        prompt = _INSTRUCCION_SINTESIS + crudos
        resp = await asyncio.to_thread(
            provider.complete, prompt, system="", max_tokens=2000,
        )
        logger.info("[synthesizer] informe combinado (%d specialists ok)", equipo.n_ok)
        return resp.text.strip() + nota_caidos
    except Exception as e:  # noqa: BLE001 — si la síntesis falla, devolver crudos (no nada)
        logger.warning("[synthesizer] falló (%s) → devuelvo bloques crudos", type(e).__name__)
        return (
            "📋 Informe del equipo (sin combinar — la síntesis no estuvo disponible):\n\n"
            + crudos + nota_caidos
        )


# ============================================================================
# S7 — CAPAS DE MEMORIA: que el equipo no explote la RAM (R5 B3, 18 capas grupo C)
# ============================================================================
#
# Para el setup actual (single-user, server ~16GB libres, cada specialist consume
# poco), el grueso del riesgo de RAM ya lo cubren DOS protecciones existentes:
#   · CONCURRENCIA_MAX=2 (S4): máximo 2 specialists en RAM a la vez, no los 5+.
#   · backpressure del MessageBus (S3): las colas tienen maxsize → no desbordan.
# Este sub-paso AÑADE: medición de RAM + bounds declarativos + alerta de umbral.
# Restart preventivo / leak forensics = producción multi-tenant (preparado, no activo
# hoy: systemd ya recicla el proceso si muriera).

# Bounds declarativos (lo que el diseño LOCKED pide explícito). Hoy se aplican vía
# CONCURRENCIA_MAX (RAM) + max_tokens por specialist (S1, acota su contexto/salida).
RAM_ALERTA_MB = 6000   # si el proceso pasa esto, log de alerta (server ~19GB, BGE-M3 ~2.6GB)


def uso_memoria_mb() -> int:
    """RSS (memoria física) del proceso ahora, en MB. Vía resource (stdlib, sin
    dependencias). En Linux ru_maxrss viene en KB → /1024. Es el PICO del proceso."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def chequear_ram() -> tuple[int, bool]:
    """Devuelve (ram_mb, alerta). alerta=True si supera RAM_ALERTA_MB → loguea.
    Pensado para llamarse antes/después de una corrida del equipo (anticipo del
    RSS threshold alert del diseño LOCKED)."""
    mb = uso_memoria_mb()
    alerta = mb > RAM_ALERTA_MB
    if alerta:
        logger.warning("[memoria] RSS %d MB supera el umbral %d MB", mb, RAM_ALERTA_MB)
    return mb, alerta
