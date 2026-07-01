# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""Canal Telegram de For3s OS (H3) — el bot que conecta tu celular al cerebro.

Reusa el cerebro completo (Conversation = memoria H2 + Claude H1 + audit):
este módulo solo es la "puerta de entrada" de Telegram.

Patrón: POLLING (sin puertos públicos) + allowlist FAIL-CLOSED ("sin lista =
denegar por default") + split de respuestas a 4,096 chars.

Acceso (decisión de diseño 2026-06-11): el PRIMER /start registra al dueño;
después, todo lo demás queda bloqueado. El dueño comparte memoria con el CLI
(sesión "brian"). Multi-usuario formal llega en H13 (auth/RBAC).

Cupo (decisión de diseño 2026-06-11): cada respuesta muestra el cupo de la
suscripción usado; a partir del 80% alerta visible; /cupo lo consulta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from for3s_core import audit, db, memory, multimodal, tiempo
from for3s_core import equipo as equipo_mod
from for3s_core import temas as temas_mod
from for3s_core.agent import Agent
from for3s_core.config import load_settings
from for3s_core.conversation import (
    Conversation,
    extraer_org,
    extraer_owner_repo,
    huele_a_github,
)
from for3s_core.llm import ClaudeProvider, RateLimitExceeded, ServidorSobrecargado
from for3s_core.mcp_client import GitHubMCPClient, ejecutar_write
from for3s_core.md_html import md_a_html_telegram
from for3s_core.secret_store import SecretStore
from for3s_core.subbloques import _categoria as _categoria_archivo
from for3s_core.text_normalize import limpiar_urls, normalizar
from for3s_core.tool_loop import WRITE_TOOLS_PERMITIDAS

logger = logging.getLogger("for3s.telegram")

# ───────── MENÚ de comandos de Telegram ─────────
# Lo que se despliega al escribir "/". Se publica POR ROL (decisión de diseño): un
# miembro ve solo lo básico; el dueño/encargado ve todo. Funciona igual en modo
# normal single-owner (el dueño ve todo) que en equipo. La descripción aparece
# al lado del comando en el menú.
_MENU_BASICO = [
    BotCommand("start", "Iniciar y ver qué puedo hacer"),
    BotCommand("ayuda", "❓ Qué puedo hacer y cómo resolver problemas"),
    BotCommand("cupo", "Ver cuánto queda de la suscripción"),
    BotCommand("estado", "Salud del agente (uptime, modelo)"),
    BotCommand("version", "Versión, hito y novedades de For3s"),
    BotCommand("perfil", "Ver o editar tu perfil (rol, preferencias)"),
    BotCommand("skills", "Ver las skills (recetas) de For3s"),
    BotCommand("aprende", "🧠 Aprender una skill de lo que trabajamos"),
    BotCommand("tema", "Crear o cambiar de tema (hilo)"),
    BotCommand("temas", "Ver y elegir entre tus temas"),
    BotCommand("hilos", "Ver tus hilos y su actividad"),
]
_MENU_ADMIN = _MENU_BASICO + [
    BotCommand("model", "Elegir el modelo de IA (Haiku/Sonnet/Opus)"),
    BotCommand("autogen", "🛡️ Auto-generación de skills (on/off/status)"),
    BotCommand("dmn", "🌙 DMN: trabaja solo cuando estás inactivo"),
    BotCommand("invitar", "🚪 Abrir o cerrar la puerta del equipo"),
    BotCommand("miembros", "Ver quién está en el equipo"),
    BotCommand("salud", "🩺 Salud completa del sistema (PR2)"),
    BotCommand("datos", "📊 Analítica de uso (PR3)"),
    BotCommand("diagnostico", "Mini-reporte de actividad reciente"),
    BotCommand("reconectar", "🔌 Reconectar y verificar integraciones"),
    BotCommand("reiniciar", "Reinicio suave (reconecta GitHub)"),
    BotCommand("reiniciar_duro", "Reinicio completo del proceso"),
]

# Límite real de Telegram por mensaje (partir respuestas largas).
MAX_MESSAGE_LENGTH = 4096

# A partir de este % de cupo usado (suscripción, ventana 5h) → alerta visible.
ALERT_THRESHOLD = 0.80

# Timeouts (segundos) para no quedar congelados en operaciones largas.
GITHUB_TIMEOUT = 60  # traer el recurso de GitHub (+ lint sandbox)
# H-A: timeout de SEGURIDAD (no de "tardó demasiado"). Antes 120s ABORTABA a
# los 2 min. Se subió a 480s pero 8 min era DEMASIADO: ante un rate-limit con
# backoff, el bot quedaba congelado 8 min sin avisar (bug visto 06-14). 180s
# (3 min): margen amplio (análisis reales ~60s) pero corta y avisa pronto si
# algo se cuelga. Combinado con el fix de que RateLimitExceeded SÍ se propaga.
ANALYSIS_TIMEOUT = 180
# Anexo R3: análisis de repo COMPLETO (archivo por archivo, fila de 1) puede
# tardar varios minutos sin saturar (es por uso, no ráfaga). Timeout amplio.
REPO_TIMEOUT = 900  # 15 min
TYPING_REFRESH = 4  # cada cuántos seg re-enviar "escribiendo..." (Telegram lo apaga a los ~5s)
MAX_EN_COLA = 3  # Parte B: máx tareas GitHub esperando turno; más allá, se rechaza
# H-A: umbral para avisar "esto puede tardar" — toda tarea que use tools de
# GitHub (huele_a_github) manda un aviso inicial, porque suelen tardar 30-60s.


async def _mantener_typing(bot, chat_id: int) -> None:
    """Mantiene VIVO el indicador 'escribiendo...' hasta que se cancele.

    Telegram apaga el typing a los ~5s por cada send_chat_action. Los análisis
    con MCP tardan 30-60s; sin esto, el indicador desaparece y parece que el
    bot se colgó (reportado). Esta tarea re-envía TYPING en bucle;
    on_message la cancela cuando llega la respuesta.
    """
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except asyncio.CancelledError:
                raise  # cancelación real: salir
            except Exception:
                # BUG 2026-06-17: la red de casa parpadea → send_chat_action lanza
                # TimedOut, que ANTES escapaba el while y mataba la tarea con
                # traceback. Ahora se traga: el typing reintenta en la próxima
                # vuelta cuando la red vuelva. No debe tumbar nada.
                pass
            await asyncio.sleep(TYPING_REFRESH)
    except asyncio.CancelledError:
        pass  # cancelada al terminar el proceso: salida limpia


async def _responder_seguro(msg, texto: str) -> None:
    """Manda un mensaje al usuario reintentando si Telegram falla.

    H-A/UX: NUNCA dejar al usuario sin desenlace. La red del servidor (casa)
    parpadea, así que reintentamos VARIAS veces con espera creciente, para
    aguantar baches de red de varios segundos. Si aun así falla, lo registra.
    """
    espera = 2
    for intento in range(5):  # 5 intentos (antes 3): baches de red de casa más largos
        try:
            await msg.reply_text(texto)
            return
        except Exception:
            if intento < 4:
                await asyncio.sleep(espera)
                espera = min(espera * 2, 20)  # backoff: 2,4,8,16,20s — aguanta cortes largos
            else:
                logger.warning("no pude entregar el mensaje al usuario tras 5 intentos (red)")


async def _enviar_html(msg, texto: str) -> None:
    """Envía un texto (markdown de Claude) a Telegram como HTML renderizado.

    Convierte MD→HTML (negrita, código en <pre>/<code>, etc.) y manda con
    parse_mode=HTML. Si Telegram RECHAZA el HTML (mal formado), cae a texto
    plano limpiando los marcadores — el mensaje SIEMPRE llega (anotación #2
    de Nota: el MD se veía crudo; ahora el código sale en bloque de verdad).
    """
    # CLAVE: partir el MARKDOWN primero, convertir cada trozo a HTML COMPLETO.
    # (Bug 2026-06-15: antes se partía el HTML ya hecho → split cortaba un tag a
    # la mitad → parse fallaba → caía a texto plano que NO quita tags → se veían
    # <b>/<code> crudos. Partir el MD antes garantiza HTML balanceado por chunk.)
    import re as _re

    # Partir el MD a ~3000 (no 4096): el HTML CRECE con los tags <b>/<code> y
    # podría pasar de 4096 → Telegram lo rechazaría → fallback a texto plano →
    # tags crudos (bug 2026-06-15). 3000 deja margen para el crecimiento HTML.
    for trozo_md in split_message(texto, limit=3000):
        chunk_html = md_a_html_telegram(trozo_md)
        plano = _re.sub(r"<[^>]+>", "", chunk_html)
        plano = plano.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        # Reintentar por chunk con backoff: distingue fallo de RED (reintenta) de
        # fallo de HTML (texto plano). Red de casa parpadea → sin esto un bache
        # perdía el reporte (bug 2026-06-17).
        enviado = False
        espera = 2
        for intento in range(5):
            try:
                if intento == 0:
                    await msg.reply_text(chunk_html, parse_mode=ParseMode.HTML)
                else:
                    await msg.reply_text(plano)  # tras fallo: texto plano (cubre HTML malo Y red)
                enviado = True
                break
            except (NetworkError, TimedOut):
                await asyncio.sleep(espera)
                espera = min(espera * 2, 20)
            except Exception:
                try:
                    await msg.reply_text(plano)  # HTML mal formado (no red)
                    enviado = True
                    break
                except (NetworkError, TimedOut):
                    await asyncio.sleep(espera)
                    espera = min(espera * 2, 20)
        if not enviado:
            logger.warning("no pude entregar un chunk del reporte tras 5 intentos (red)")


def md_to_telegram(text: str) -> str:
    """Limpia los marcadores de markdown crudos para que no se vean en Telegram.

    NO usamos parse_mode de Telegram porque rechaza el mensaje entero si el
    markdown está mal balanceado (un * suelto de Claude). En vez de eso,
    quitamos los marcadores (**negrita**, *cursiva*, `code`) → texto limpio,
    sin asteriscos/backticks de ruido, y sin riesgo de que falle el envío.
    """
    import re

    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **negrita**
    text = re.sub(r"__(.+?)__", r"\1", text)  # __negrita__
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", text)  # *cursiva*
    text = re.sub(r"`([^`\n]+?)`", r"\1", text)  # `code` inline
    return text


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Parte un texto en pedazos <= limit, cortando por párrafo/línea si se puede."""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        # preferir cortar en salto de párrafo, luego de línea, luego espacio
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def _humanizar_fecha(dt) -> str:
    """AI7 — fecha relativa amigable para /hilos: 'hoy', 'ayer', 'hace 3 días',
    'hace 2 sem'. Defensiva: si no hay fecha, 'sin actividad'."""
    if dt is None:
        return "sin actividad"
    try:
        from datetime import UTC, datetime

        ahora = datetime.now(UTC)
        d = dt if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=UTC)
        seg = (ahora - d).total_seconds()
        if seg < 3600:
            return "hace un momento"
        if seg < 86400 and ahora.date() == d.date():
            return "hoy"
        dias = (ahora.date() - d.date()).days
        if dias == 1:
            return "ayer"
        if dias < 7:
            return f"hace {dias} días"
        if dias < 30:
            return f"hace {dias // 7} sem"
        return f"hace {dias // 30} mes(es)"
    except (AttributeError, TypeError, ValueError):
        return "sin actividad"


def format_cupo(usage_5h: float | None, usage_7d: float | None) -> str:
    """Pie de cupo de la suscripción. Alerta a partir del 80% usado.

    usage_* viene 0..1 (fracción usada). Devuelve "" si no hay dato.
    """
    if usage_5h is None:
        return ""
    pct = int(round(usage_5h * 100))
    libre = 100 - pct
    if usage_5h >= ALERT_THRESHOLD:
        return (
            f"⚠️ CUPO 5h al {pct}% usado — te queda {libre}%. "
            "Si llega a 100% espero a que se reinicie (no se pierde nada)."
        )
    extra = ""
    if usage_7d is not None:
        extra = f" · 7d: {int(round(usage_7d * 100))}%"
    return f"🔋 cupo 5h: {pct}% usado{extra}"


class CupoPinStore:
    """Recuerda, entre reinicios, qué mensaje de cupo está fijado por chat.

    Sin esto, cada reinicio del bot crearía una burbuja de cupo nueva. Con
    esto, el bot reusa el pin existente y SOLO lo edita → una sola burbuja
    en toda la vida de la conversación.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[int, int]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return {int(k): int(v) for k, v in raw.items()}
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {}

    def save(self, data: dict[int, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({str(k): v for k, v in data.items()}), encoding="utf-8")
        self._path.chmod(0o600)


class OwnerStore:
    """Guarda quién es el dueño del bot (el primer /start). Fail-closed.

    PR6.1 (BUG-4): la BD es la FUENTE DE VERDAD del owner (tabla `owner`), porque la
    BD siempre está montada y viaja con los backups — a diferencia del JSON en cwd que
    rompió la migración ("Foresito olvidó todo"). El JSON queda como caché/compat. Hay
    un CACHÉ en memoria (`_cache`) para no leer disco en las ~18 llamadas por turno.

    Orden de verdad: caché memoria → (en setup) BD → JSON. get_owner() es síncrono
    (compat con las 18 llamadas); sync_con_bd() se llama 1 vez en setup() para cargar
    el owner de la BD a la caché + reparar el JSON si hace falta."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: int | None = None  # PR6.1: owner cacheado en memoria

    def _leer_json(self) -> int | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            owner = data.get("owner_id")
            return int(owner) if owner is not None else None
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return None

    def get_owner(self) -> int | None:
        # 1º la caché (rápido, robusto); 2º el JSON (fallback). La BD se sincronizó
        # a la caché en setup() — por eso get_owner sigue siendo síncrono.
        if self._cache is not None:
            return self._cache
        owner = self._leer_json()
        if owner is not None:
            self._cache = owner
        return owner

    async def sync_con_bd(self, pool) -> None:
        """PR6.1: en setup(), la BD manda. Carga el owner de la tabla `owner` a la
        caché y, si el JSON está desincronizado/ausente, lo repara. Defensivo: si la
        BD falla, cae al JSON (no peor que antes). Esto CIERRA BUG-4: aunque el JSON
        se pierda (migración), el owner se recupera de la BD."""
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT owner_id FROM owner WHERE workspace = 'default'"
                )
            if row is not None:
                self._cache = int(row)
                # reparar el JSON si no coincide (compat con código que lo lea directo)
                if self._leer_json() != self._cache:
                    self._escribir_json(self._cache)
                logger.info("[owner] cargado de la BD: %s (fuente de verdad)", self._cache)
                return
            # la BD no tiene owner aún: si el JSON sí, migrarlo a la BD
            json_owner = self._leer_json()
            if json_owner is not None:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO owner (workspace, owner_id) VALUES ('default', $1) "
                        "ON CONFLICT (workspace) DO UPDATE SET owner_id=EXCLUDED.owner_id",
                        json_owner,
                    )
                self._cache = json_owner
                logger.info("[owner] migrado JSON→BD: %s", json_owner)
        except Exception as e:  # noqa: BLE001 — si la BD falla, seguimos con el JSON
            logger.warning("[owner] sync_con_bd falló (uso JSON): %s", type(e).__name__)
            self._cache = self._leer_json()

    def _escribir_json(self, user_id: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"owner_id": user_id}), encoding="utf-8")
        self._path.chmod(0o600)

    def set_owner(self, user_id: int) -> None:
        # escribe JSON + caché; la BD se actualiza en set_owner_bd (async, desde setup/start)
        self._escribir_json(user_id)
        self._cache = user_id

    async def set_owner_bd(self, pool, user_id: int) -> None:
        """Persiste el owner en la BD (fuente de verdad) + JSON + caché. PR6.1."""
        self.set_owner(user_id)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO owner (workspace, owner_id) VALUES ('default', $1) "
                    "ON CONFLICT (workspace) DO UPDATE SET owner_id=EXCLUDED.owner_id, "
                    "actualizado_at=now()",
                    user_id,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("[owner] set_owner_bd falló (queda en JSON): %s", type(e).__name__)

    async def transferir(self, pool, nuevo_owner_id: int) -> tuple[bool, str]:
        """PR6.2a: transferir el dueño a otra persona, de forma ATÓMICA en los 3
        lugares que importan, para no DESINCRONIZAR (bug entre componentes):
          1. tabla `owner` (la fuente de verdad)
          2. `equipos.encargado_id` (el equipo del viejo owner → pasa al nuevo, si existe;
             sin esto, la PUERTA del equipo dejaría de funcionar para el nuevo dueño)
          3. JSON + caché en memoria
        Todo en UNA transacción: o los 3 o ninguno (rollback). Devuelve (ok, motivo)."""
        viejo = self.get_owner()
        if nuevo_owner_id == viejo:
            return False, "ya_es_dueno"
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # 1. owner (fuente de verdad)
                    await conn.execute(
                        "INSERT INTO owner (workspace, owner_id) VALUES ('default', $1) "
                        "ON CONFLICT (workspace) DO UPDATE SET owner_id=EXCLUDED.owner_id, "
                        "actualizado_at=now()",
                        nuevo_owner_id,
                    )
                    # 2. encargado del equipo del viejo owner → al nuevo (si existe equipo)
                    if viejo is not None:
                        await conn.execute(
                            "UPDATE equipos SET encargado_id=$1 WHERE encargado_id=$2",
                            nuevo_owner_id,
                            viejo,
                        )
            # 3. JSON + caché (fuera de la transacción de BD, ya commiteada)
            self.set_owner(nuevo_owner_id)
            logger.info("[owner] TRANSFERIDO %s → %s (atómico)", viejo, nuevo_owner_id)
            return True, "ok"
        except Exception as e:  # noqa: BLE001 — si falla, la transacción hizo rollback
            logger.exception("[owner] transferir falló (rollback): %s", type(e).__name__)
            return False, f"error: {type(e).__name__}"

    async def recuperar(self, pool) -> tuple[bool, str]:
        """PR6.2b: re-sincroniza owner ↔ encargado ↔ JSON desde la BD (fuente de
        verdad). Red de seguridad si algo se desincronizó. Devuelve (ok, owner_id)."""
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT owner_id FROM owner WHERE workspace='default'"
                )
                if row is None:
                    return False, "sin owner en BD"
                owner = int(row)
                # re-alinear el encargado del equipo con el owner de la BD
                await conn.execute(
                    "UPDATE equipos SET encargado_id=$1 WHERE encargado_id <> $1",
                    owner,
                )
            self.set_owner(owner)  # JSON + caché
            logger.info("[owner] recuperado de la BD: %s (re-sincronizado)", owner)
            return True, str(owner)
        except Exception as e:  # noqa: BLE001
            logger.warning("[owner] recuperar falló: %s", type(e).__name__)
            return False, f"error: {type(e).__name__}"

    def is_authorized(self, user_id: int | None) -> bool:
        """FAIL-CLOSED: sin dueño registrado o sin user_id → denegado."""
        owner = self.get_owner()
        return owner is not None and user_id is not None and user_id == owner


def crear_progreso_categorias(context, chat_id, titulo_fn):
    """Crea un callback de progreso POR CATEGORÍAS (1 mensaje editable, bolas
    🟢🟡⚪, sin lista de archivos). Lo usan TANTO el mapeo inicial como la
    CONTINUACIÓN (2026-06-18: la continuación se ve igual que el inicio,
    no como spam de mensajes con lista de archivos). titulo_fn(hechos,total,prof)
    arma el encabezado (ej. 'Mapeando...' o 'Continuando...')."""
    _CAT_LABEL = {
        "readme": "README",
        "config": "Config/CI",
        "doc": "Documentación",
        "src": "Código fuente",
        "test": "Tests",
        "otro": "Otros",
    }
    _prog = {
        "total": 0,
        "hechos": 0,
        "tot_cat": {},
        "ok_cat": {},
        "curso": set(),
        "errores": [],
        "m": None,
        "ultimo": "",
        "profundo": True,
    }

    def _render() -> str:
        prof = _prog["profundo"]
        cab = titulo_fn(_prog["hechos"], _prog["total"], prof)
        lineas = []
        for cat in ("readme", "config", "doc", "src", "test", "otro"):
            tot = _prog["tot_cat"].get(cat, 0)
            if tot == 0:
                continue
            hechos = _prog["ok_cat"].get(cat, 0)
            if hechos >= tot:
                bola = "🟢"
            elif cat in _prog["curso"]:
                bola = "🟡"
            else:
                bola = "⚪"
            lineas.append(
                f"{bola} {_CAT_LABEL[cat]}  {hechos}/{tot}" if prof else f"{bola} {_CAT_LABEL[cat]}"
            )
        cuerpo = "\n".join(lineas)
        if _prog["errores"]:
            errs = "\n".join(f"🔴 {r} — {m}" for r, m in _prog["errores"][-5:])
            cuerpo += f"\n\nErrores:\n{errs}"
        return cab + cuerpo

    async def _pintar() -> None:
        txt = _render()
        if txt == _prog["ultimo"]:
            return
        _prog["ultimo"] = txt
        try:
            if _prog["m"] is None:
                _prog["m"] = await context.bot.send_message(chat_id, txt)
            else:
                await context.bot.edit_message_text(
                    txt, chat_id=chat_id, message_id=_prog["m"].message_id
                )
        except Exception:
            pass

    async def _progreso(ruta: str, estado: str, detalle: str) -> None:
        if estado == "plan":
            import json as _json

            d = _json.loads(detalle)
            _prog["total"] = d.get("total", 0)
            _prog["tot_cat"] = d.get("por_cat", {})
            _prog["profundo"] = d.get("profundo", True)
            await _pintar()
            return
        cat = _categoria_archivo(ruta)
        if estado == "curso":
            _prog["curso"].add(cat)
        elif estado == "ok":
            _prog["hechos"] += 1
            _prog["ok_cat"][cat] = _prog["ok_cat"].get(cat, 0) + 1
            _prog["curso"].discard(cat)
        elif estado == "error":
            _prog["hechos"] += 1
            _prog["ok_cat"][cat] = _prog["ok_cat"].get(cat, 0) + 1
            _prog["curso"].discard(cat)
            _prog["errores"].append((ruta, detalle or "error"))
        await _pintar()

    return _progreso


class TelegramChannel:
    """La puerta de Telegram hacia el cerebro de For3s."""

    def __init__(
        self, owner_store: OwnerStore, owner_session: str, pin_store: CupoPinStore
    ) -> None:
        self._owners = owner_store
        self._owner_session = owner_session
        self._pins = pin_store
        self._pool = None
        self._transfer_pendiente: int | None = None  # PR6.2a
        self._agent: Agent | None = None
        self._mcp: GitHubMCPClient | None = None  # sesión MCP GitHub (Paso 4-6)
        self._started_at: float = time.time()  # para /estado (uptime)
        self._model: str = "?"  # se llena en setup()
        # Parte B (anti-rate-limit): cola SERIAL para tareas GitHub. Un Lock
        # garantiza que solo UN análisis tool-use corre a la vez (no se solapan
        # ráfagas que saturarían el rate-limit). _en_cola cuenta los que esperan
        # (para el límite + feedback al usuario). La charla normal NO usa esto.
        self._gh_lock: asyncio.Lock = asyncio.Lock()
        self._en_cola: int = 0  # cuántas tareas GitHub esperan turno
        self._cupo_msg_id: dict[int, int] = pin_store.load()  # persistido entre reinicios
        self._last_cupo: tuple[float | None, float | None] = (
            None,
            None,
        )  # ultimo cupo visto (gratis)
        # ultimo TEXTO de cupo mostrado por chat -> si no cambio, ni tocamos
        # Telegram (evita el 400 "message is not modified" del Bug G).
        self._cupo_text: dict[int, str] = {}
        # WRITE TOOLS (2026-06-18): acciones de escritura PROPUESTAS y a la
        # espera del clic de confirmación. Clave = id corto (cabe en callback_data
        # de 64 bytes); valor = {name, args, user_id, ts}. Se borra al confirmar/
        # cancelar/expirar. El PAT se guarda para ejecutar la write confirmada en
        # un contenedor MCP write-capable efímero (ver mcp_client.ejecutar_write).
        self._writes_pendientes: dict[str, dict] = {}
        self._write_seq: int = 0
        self._pat: str | None = None  # PAT de GitHub (para ejecutar writes)
        # B (pulido H8): sugerencias de equipo pendientes (texto original asociado
        # al botón "¿lanzo el equipo?"). Clave = id corto; valor = {texto, user_id}.
        self._equipo_sugerido: dict[str, dict] = {}
        self._sug_seq: int = 0
        # G (robustez bajo carga): solo UNA corrida de equipo (5 specialists) a la
        # vez en TODO el bot — evita que varias personas disparen equipos en paralelo
        # y saturen el rate-limit OAuth (Tier 1). Las demás esperan turno. La charla
        # normal (1 agente) NO usa esto → sigue instantánea en paralelo.
        self._equipo_lock: asyncio.Lock = asyncio.Lock()
        self._equipos_en_cola: int = 0
        # H8 S10e — EQUIPO multi-usuario. ADITIVO Y SILENCIOSO: hasta que el dueño
        # use /invitar, no hay equipo creado y todo opera single-owner como hoy.
        self._equipo: equipo_mod.EquipoStore | None = None
        # AI2 — TEMAS por persona (shared-thread inbox). Default 'general' → opt-in.
        self._temas: temas_mod.TemaStore | None = None

    async def setup(self, app: Application) -> None:
        """post_init de PTB: conecta el cerebro (pool + provider)."""
        settings = load_settings()
        self._pool = await db.connect(settings.database_url)
        await db.apply_migrations(self._pool)
        # PR6.1 (BUG-4): la BD es la fuente de verdad del owner. Cargar a la caché +
        # reparar el JSON si hace falta (así una migración no vuelve a "olvidar" al dueño).
        await self._owners.sync_con_bd(self._pool)
        self._equipo = equipo_mod.EquipoStore(self._pool)  # H8 S10e (aditivo)
        self._temas = temas_mod.TemaStore(self._pool)  # AI2 temas (aditivo)
        provider = ClaudeProvider(
            token=settings.anthropic_token,
            oauth=settings.is_oauth,
            model=settings.model,
            # 180s (no el default 60s): un PDF/imagen grande o un repo grande hace
            # que Claude tarde más de 60s → httpcore.ReadTimeout ("error procesando
            # adjunto", 2026-06-18). Alineado con ANALYSIS_TIMEOUT.
            timeout=180.0,
        )
        self._agent = Agent(provider)
        self._model = settings.model
        # Aplicar el modelo que se eligió con /model (persistido en BD), si hay.
        # Así su selección sobrevive reinicios. Si no eligió, queda el de settings.
        try:
            from for3s_core import modelos

            elegido = await modelos.get_seleccionado(self._pool, settings.owner_session)
            if elegido and elegido != settings.model:
                provider.set_model(elegido)
                self._model = elegido
        except Exception:  # noqa: BLE001 — si falla, queda el modelo de settings
            pass
        logger.info("cerebro conectado (modelo=%s auth=%s)", self._model, settings.auth_mode)

        # MCP GitHub (Paso 4-6): sesión persistente, PAT del SecretStore (KEK).
        # Si falla (Docker abajo, etc.) el bot sigue: degrada a sin-GitHub.
        try:
            pat = await SecretStore(self._pool).get_secret(settings.owner_session, "github_token")
            self._pat = pat  # para ejecutar writes confirmadas (contenedor efímero)
            mcp = GitHubMCPClient(pat, read_only=True)
            await mcp.start()
            self._mcp = mcp
            logger.info("GitHub MCP conectado (read-only)")
        except Exception:
            logger.exception("no pude iniciar el GitHub MCP (sigo sin GitHub)")
            self._mcp = None

        # PRECARGA del modelo de embeddings en BACKGROUND (H5 sub-paso 8). BGE-M3
        # tarda ~160s en cargar a RAM la 1ª vez. Si esa carga ocurriera dentro del
        # 1er mensaje (al llamar buscar_semantico), ese mensaje tardaría 160s. Lo
        # precargamos en una tarea aparte → el bot arranca ya, el modelo se carga
        # en paralelo, y para cuando llegue el 1er mensaje normalmente ya está
        # listo. Defensivo: si falla, la memoria semántica degrada (no rompe nada).
        async def _precargar_embeddings() -> None:
            try:
                from for3s_core import embeddings

                await asyncio.to_thread(embeddings._get_modelo)
                logger.info("modelo de embeddings precargado (memoria semántica lista)")
            except Exception:
                logger.warning(
                    "no pude precargar embeddings (memoria semántica degradará)", exc_info=True
                )

        asyncio.create_task(_precargar_embeddings())

        # MENÚ de comandos por defecto (lo que ve quien aún no es admin). El menú
        # ADMIN se publica por-chat al dueño/encargado. Defensivo.
        try:
            await app.bot.set_my_commands(_MENU_BASICO, scope=BotCommandScopeDefault())
            logger.info("menú de comandos por defecto publicado")
            # Publicar YA el menú ADMIN del DUEÑO (sin esperar a que interactúe):
            # en chat privado el chat_id == user_id, así que su menú completo
            # existe desde el arranque. Encargados de equipo se actualizan al
            # escribir (on_message). Esto arregla el "solo veo 3 comandos".
            owner = self._owners.get_owner()
            if owner is not None:
                await app.bot.set_my_commands(_MENU_ADMIN, scope=BotCommandScopeChat(owner))
                logger.info("menú ADMIN publicado para el dueño %s", owner)
        except Exception:  # noqa: BLE001 — menú cosmético, no bloquea el arranque
            logger.warning("no pude publicar el menú (no crítico)", exc_info=True)

    async def teardown(self, app: Application) -> None:
        """post_shutdown de PTB: cierra el pool de BD ordenadamente.

        Esto SÍ se await-ea bien en el shutdown (verificado: sin errores de
        pool al apagar). Nota: al recibir SIGTERM verás en los logs
        "RuntimeWarning: coroutine 'Updater.stop' was never awaited" — es un
        warning COSMÉTICO interno de python-telegram-bot 22.x: en la carrera
        de la señal, PTB evalúa self.updater.running y la corrutina stop()
        queda sin await. NO es bug nuestro; el apagado es correcto (pool y
        application se cierran). Decisión (el dueño, 2026-06-13): dejarlo, no
        vale la pena un shutdown manual solo por cosmética.
        """
        if self._mcp is not None:
            try:
                await self._mcp.aclose()
            except Exception:
                logger.warning("error cerrando el GitHub MCP (no crítico)")
        if self._pool is not None:
            await self._pool.close()

    # ---- handlers ----

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Error handler GLOBAL (bug 2026-06-17: la red del servidor a Telegram
        parpadea — ConnectTimeout/TimedOut — y SIN este handler cada error:
        (a) ensuciaba los logs con un traceback crudo ('No error handlers
        registered'), y (b) podía tumbar tareas. Aquí los errores de RED se
        tragan con un log corto (el bot sigue vivo; el polling reintenta solo).
        Otros errores SÍ se registran con detalle para depurar."""
        err = context.error
        if isinstance(err, (NetworkError, TimedOut)):
            # Bache de red con Telegram: cosmético, el polling se recupera solo.
            logger.warning("Red Telegram inestable (%s) — reintenta solo", type(err).__name__)
            return
        # Cualquier otro error: log con detalle (pero NO se propaga → bot vivo)
        logger.error("Error no manejado: %s", err, exc_info=err)
        # PR10.3b: ADEMÁS de loguear, AVISAR al usuario (no dejarlo esperando en
        # silencio). Solo si el update trae un mensaje al que responder. Defensivo:
        # el propio aviso nunca debe causar otro error (de ahí el try y _responder_seguro).
        try:
            msg = getattr(update, "message", None) if isinstance(update, Update) else None
            if msg is not None:
                await _responder_seguro(
                    msg,
                    "❌ Algo falló de mi lado procesando eso (no fue tu mensaje). "
                    "Ya quedó registrado. Reintenta; si sigue, el dueño puede usar "
                    "/salud o /reconectar.",
                )
        except Exception:  # noqa: BLE001 — el aviso de error JAMÁS debe romper el handler
            logger.warning("on_error: no pude avisar al usuario (no crítico)")

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return
        owner = self._owners.get_owner()
        if owner is None:
            await self._owners.set_owner_bd(self._pool, user.id)  # PR6.1: persiste en BD
            logger.info("dueño registrado: %s (%s)", user.id, user.full_name)
            await self._publicar_menu(context.bot, update.message.chat_id, user)
            await update.message.reply_text(
                "👑 Quedaste registrado como dueño de For3s OS.\n"
                "Escríbeme lo que quieras — recuerdo nuestras conversaciones.\n"
                "Escribe / para ver todos mis comandos."
            )
            return
        # H8: autorización ADITIVA (dueño / miembro / puerta). Publica el menú
        # según el rol y saluda. Si no está autorizado, mensaje según la puerta.
        ok, motivo = await self._autorizar(user)
        if not ok:
            if motivo == "puerta_cerrada":
                await update.message.reply_text(
                    "🔴 La puerta de este equipo está cerrada. Pídele al encargado "
                    "que la abra (con /invitar) para entrar."
                )
            else:
                await update.message.reply_text("⛔ Este bot es privado.")
            return
        await self._publicar_menu(context.bot, update.message.chat_id, user)
        if self._es_admin(user.id):
            await update.message.reply_text(
                "🦊 Hola de nuevo. Te escucho. Escribe / para ver tus comandos."
            )
        else:
            await update.message.reply_text(
                "🦊 ¡Hola! Soy For3s, el segundo cerebro del equipo. Escríbeme lo que "
                "necesites. Escribe / para ver los comandos disponibles."
            )

    async def _update_cupo_pin(self, context, chat_id, usage_5h, usage_7d) -> None:
        """Mantiene SOLO el mensaje fijado de arriba con el cupo, sin ruido.

        - Si ya existe: lo EDITA (cero burbujas nuevas, cero avisos).
        - Si no existe: lo crea, lo fija en silencio, y borra tanto la burbuja
          del chat como el aviso "fijó ..." → solo queda el pin de arriba.
        Robusto: cualquier fallo de Telegram no rompe la conversación.
        """
        self._last_cupo = (usage_5h, usage_7d)
        text = format_cupo(usage_5h, usage_7d)
        if not text:
            return
        existing = self._cupo_msg_id.get(chat_id)
        if existing is not None:
            # Bug G: si el texto NO cambio desde la ultima vez, no tocar Telegram.
            # Editar a un texto identico da 400 "message is not modified" → ruido
            # + recreacion del pin + parpadeo. Si es igual, ya esta mostrado: salir.
            if self._cupo_text.get(chat_id) == text:
                return
            try:
                await context.bot.edit_message_text(text, chat_id=chat_id, message_id=existing)
                self._cupo_text[chat_id] = text
                return
            except BadRequest as e:
                # 400 "not modified": el pin ya muestra este texto (inofensivo).
                if "not modified" in str(e).lower():
                    self._cupo_text[chat_id] = text
                    return
                self._cupo_msg_id.pop(chat_id, None)
                self._cupo_text.pop(chat_id, None)
            except Exception:
                self._cupo_msg_id.pop(chat_id, None)
                self._cupo_text.pop(chat_id, None)
        try:
            sent = await context.bot.send_message(chat_id=chat_id, text=text)
            self._cupo_msg_id[chat_id] = sent.message_id
            self._cupo_text[chat_id] = text  # Bug G: recordar lo mostrado
            self._pins.save(self._cupo_msg_id)
            # fijar en silencio (genera un service message "fijó ...")
            await context.bot.pin_chat_message(
                chat_id=chat_id, message_id=sent.message_id, disable_notification=True
            )
            # borrar SOLO el aviso de sistema "For3s OS fijó ..." (service msg,
            # suele ser message_id+1). La burbuja del cupo NO se borra: si se
            # borrara, el pin se iría con ella. Pero solo se crea UNA vez por
            # chat — en adelante se EDITA (sin nuevas burbujas ni avisos).
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=sent.message_id + 1)
            except Exception:
                pass
        except Exception:
            logger.warning("no se pudo fijar el mensaje de cupo (no crítico)")

    def _base_sesion(self, user) -> str:
        """Hilo BASE de la persona (#6), SIN tema. Dueño→'brian' (conserva
        historial); otros→'tg:<uid>'. Sin user→dueño (compat)."""
        uid = getattr(user, "id", None)
        if uid is None or self._owners.is_authorized(uid):
            return self._owner_session
        return f"tg:{uid}"

    async def _sesion_de(self, user) -> str:
        """#6 HILO POR USUARIO + AI2 TEMAS: session_id de ESTA persona EN SU TEMA
        ACTIVO. Hilo base por persona (#6) + sufijo del tema (AI2). El tema
        'general' NO añade sufijo → el hilo base ('brian') = tema general del
        dueño y CONSERVA su historial. Otros temas → 'brian:backend', etc.
        Fail-safe: si temas falla, cae al hilo base (= comportamiento del #6)."""
        base = self._base_sesion(user)
        uid = getattr(user, "id", None)
        if uid is None or self._temas is None:
            return base
        tema = await self._temas.activo(uid)
        if tema == temas_mod.TEMA_DEFAULT:
            return base  # tema general → hilo base (conserva historial)
        return f"{base}:{tema}"

    def _scope_de(self, user) -> int | None:
        """AI1 DOCTRINA DE AISLAMIENTO: el scope_user_id para filtrar la búsqueda
        semántica. DUEÑO → None (ve TODO lo suyo, incl. su legado owner_user_id=NULL,
        compat). MIEMBRO → su user_id (solo su privada + la común del equipo, NUNCA
        lo privado de otra persona). 2ª capa de aislamiento sobre el session_id."""
        uid = getattr(user, "id", None)
        if uid is None or self._owners.is_authorized(uid):
            return None  # dueño/CLI: sin filtro (ve todo lo suyo)
        return uid  # miembro: aislado a lo suyo + común

    async def _autorizar(self, user) -> tuple[bool, str]:
        """Autorización ADITIVA (H8 S10e). Combina el modo single-owner de hoy
        (OwnerStore) con el EQUIPO (EquipoStore + puerta). FAIL-CLOSED:

          1. Dueño (OwnerStore) → pasa (compat total: si nunca usaste /invitar,
             aquí termina y todo sigue como hoy).
          2. Si hay equipo, delega en EquipoStore.autorizar (miembro / puerta).
          3. Si no, denegado.

        Devuelve (autorizado, motivo). NO lanza: ante cualquier error, deniega."""
        uid = getattr(user, "id", None)
        if self._owners.is_authorized(uid):
            return True, "dueño"
        if self._equipo is None:
            return False, "privado"
        try:
            nombre = getattr(user, "full_name", None)
            return await self._equipo.autorizar(self._owners.get_owner(), uid, nombre=nombre)
        except Exception:  # noqa: BLE001 — fail-closed: ante error, denegar
            logger.warning("error en _autorizar (deniego)", exc_info=True)
            return False, "error"

    async def _publicar_menu(self, bot, chat_id: int, user) -> None:
        """Publica el menú de comandos '/' para ESTE chat según el ROL del usuario
        (decisión de diseño: el comando aparece según el rol). Dueño/encargado → menú
        ADMIN completo; miembro → menú BÁSICO. Idempotente y defensivo (si falla,
        no rompe nada; el menú es cosmético). Se llama al primer contacto y en cada
        mensaje autorizado (Telegram cachea, así que re-publicar es barato)."""
        try:
            comandos = _MENU_ADMIN if self._es_admin(getattr(user, "id", None)) else _MENU_BASICO
            await bot.set_my_commands(comandos, scope=BotCommandScopeChat(chat_id))
        except Exception:  # noqa: BLE001 — el menú es cosmético, nunca debe romper
            logger.warning("no pude publicar el menú de comandos (no crítico)")

    async def _bienvenida_y_aviso(self, context, msg, user) -> None:
        """C (pulido H8): alguien ACABA DE ENTRAR por la puerta. C-ii: saluda al que
        entra. C-i: avisa al ENCARGADO (mensaje proactivo a su chat) quién entró.
        DEFENSIVO: cada parte en su try — nunca rompe el flujo del mensaje."""
        nombre = getattr(user, "full_name", None) or "Alguien"
        # C-ii — bienvenida al que entra
        try:
            await msg.reply_text(
                f"👋 ¡Bienvenida/o al equipo, {nombre}! Soy For3s, el segundo cerebro "
                "compartido. Escríbeme lo que necesites; tu conversación es privada "
                "y separada de la de los demás."
            )
        except Exception:  # noqa: BLE001
            pass
        # C-i — aviso PROACTIVO al encargado (a su chat, por su user_id)
        owner_id = self._owners.get_owner()
        if owner_id is not None and owner_id != getattr(user, "id", None):
            try:
                n_mie = 0
                if self._equipo is not None:
                    eid = await self._equipo.equipo_de(owner_id)
                    if eid is not None:
                        n_mie = len(await self._equipo.miembros(eid))
                extra = f" · ahora son {n_mie}" if n_mie else ""
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=f"👤 *{nombre}* se unió al equipo (por la puerta abierta){extra}.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:  # noqa: BLE001 — el encargado pudo bloquear al bot, etc.
                logger.warning("no pude avisar al encargado del nuevo miembro")

    # ───────── H8 S11: motor multi-agente AUTOMÁTICO + gobernado ─────────

    def _amerita_equipo(self, texto: str) -> bool:
        """¿Esta tarea amerita lanzar el EQUIPO multi-agente (5 specialists)?
        Detector CONSERVADOR (decisión de diseño: automático pero sin gastos sorpresa).
        Solo dispara con señales FUERTES de análisis multi-ángulo profundo. La
        charla normal y preguntas simples NUNCA caen aquí → siguen con 1 agente.
        Las 7 capas de cost_control gobiernan el costo una vez dentro."""
        t = normalizar(texto)
        # señales explícitas de querer un análisis amplio/multi-perspectiva
        gatillos = (
            "analiza a fondo",
            "analisis a fondo",
            "analiza a profundidad",
            "revision completa",
            "revisa a fondo",
            "auditoria completa",
            "auditoria de",
            "analisis completo",
            "evalua a fondo",
            "revision exhaustiva",
            "analisis exhaustivo",
            "todos los angulos",
            "desde todos los angulos",
            "vision completa",
            "analiza con el equipo",
            "usa el equipo",
            "lanza el equipo",
        )
        return any(g in t for g in gatillos)

    def _sugiere_equipo(self, texto: str) -> bool:
        """B (pulido H8): ¿la tarea SE BENEFICIARÍA del equipo, sin ser un gatillo
        directo? Señales más suaves (comparar, evaluar, riesgos, pros/contras,
        decidir entre opciones, analizar algo). Si True → el bot OFRECE un botón
        (no lanza solo) → cero gasto sorpresa + descubrible. CUIDADOSO: requiere
        un mínimo de longitud y NO dispara en saludos/preguntas triviales."""
        t = normalizar(texto)
        if len(t) < 15:
            return False  # mensajes muy cortos = charla, no amerita
        señales = (
            "compara",
            "comparar",
            "comparacion",
            "pros y contras",
            "ventajas y desventajas",
            "evalua",
            "evaluar",
            "evaluacion",
            "que riesgos",
            "riesgos de",
            "que tan",
            "cual es mejor",
            "cual conviene",
            "decidir entre",
            "que opcion",
            "ayudame a decidir",
            "analiza",
            "analisis de",
            "revisa",
            "audita",
            "que opinas de",
            "plan para",
            "estrategia para",
        )
        return any(s in t for s in señales)

    async def _correr_equipo_y_responder(
        self, msg, texto: str, scope_user_id=None, *, sesion=None, autor_id=None
    ) -> None:
        """G (robustez): garantiza que solo UNA corrida de equipo corre a la vez en
        todo el bot (semáforo global). Si hay una en curso, avisa que está en cola y
        espera turno. Luego delega en _correr_equipo_inner. Así varias personas NO
        disparan equipos en paralelo (anti-429 OAuth)."""
        MAX_EQUIPOS_COLA = 3
        if self._equipos_en_cola >= MAX_EQUIPOS_COLA:
            await _responder_seguro(
                msg,
                "📋 Hay varios análisis de equipo en espera. Dame un momento a "
                "que bajen y reintenta — así no saturo el límite de Claude.",
            )
            return
        if self._equipo_lock.locked():
            await _responder_seguro(
                msg,
                "📋 Hay un análisis de equipo en curso — el tuyo entra en cola, "
                "lo proceso en cuanto termine (no tienes que repetir).",
            )
        self._equipos_en_cola += 1
        try:
            async with self._equipo_lock:
                await self._correr_equipo_inner(
                    msg, texto, scope_user_id=scope_user_id, sesion=sesion, autor_id=autor_id
                )
        finally:
            self._equipos_en_cola -= 1
        # H12 P2: una corrida de equipo = tarea compleja → buen candidato a destilar
        # una skill. Se dispara en BACKGROUND (no bloquea la respuesta) y pasa por el
        # governor: si /autogen está OFF (default), can_generate niega sin gastar
        # tokens. Solo si el dueño lo encendió, propone la skill al gate.
        asyncio.create_task(self._auto_mejora_background(msg, sesion, autor_id))

    async def _auto_mejora_background(self, msg, sesion, autor_id) -> None:
        """H12 P2: en background, pregunta si vale guardar una skill de la tarea
        recién hecha. Frenado por el governor (kill switch). Si propone una, la deja
        en 'stale' y avisa al DUEÑO con botones ✅/❌ (gate). Nunca toca el chat
        principal salvo ese aviso. Defensivo: jamás rompe nada."""
        try:
            if self._pool is None or self._agent is None:
                return
            provider = getattr(self._agent, "_provider", None)
            if provider is None or sesion is None:
                return
            from for3s_core.aprende import proponer_skill_auto

            res = await proponer_skill_auto(self._pool, provider, sesion, creada_por=autor_id)
            if not (res.ok and res.requiere_gate and res.skill_id is not None):
                return  # frenada por kill switch o no valía → silencio
            owner_id = self._owners.get_owner()
            if owner_id is None:
                return
            teclado = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Activar", callback_data=f"skok:{res.skill_id}"),
                        InlineKeyboardButton("❌ Descartar", callback_data=f"skno:{res.skill_id}"),
                    ]
                ]
            )
            await msg.get_bot().send_message(
                chat_id=owner_id,
                text=(
                    f"🤖 Aprendí algo y propongo una skill nueva: *{res.nombre}* "
                    f"({res.categoria}).\nMírala con `/skills {res.nombre}` y decide:"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=teclado,
            )
        except Exception:  # noqa: BLE001 — la auto-mejora NUNCA tumba el bot
            logger.warning("auto-mejora en background falló (ignoro)", exc_info=True)

    async def on_skill_gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """H12 P2: el dueño aprueba (skok) o descarta (skno) una skill auto-propuesta.
        Aprobar → active (entra en uso). Descartar → archived (recuperable)."""
        q = update.callback_query
        if q is None or not q.data or self._pool is None:
            return
        await q.answer()
        if not self._es_admin(q.from_user.id if q.from_user else None):
            await q.edit_message_text("⛔ Solo el dueño decide sobre las skills propuestas.")
            return
        accion, _, sid_str = q.data.partition(":")
        try:
            sid = int(sid_str)
        except ValueError:
            return
        from for3s_core.aprende import aprobar_skill, rechazar_skill

        if accion == "skok":
            nombre = await aprobar_skill(self._pool, sid)
            await q.edit_message_text(
                f"✅ Skill *{nombre or 'propuesta'}* activada. La usaré cuando aplique."
                if nombre
                else "⚠️ Esa skill ya no estaba pendiente.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            nombre = await rechazar_skill(self._pool, sid)
            await q.edit_message_text(
                f"🗑️ Skill *{nombre or 'propuesta'}* descartada (archivada, recuperable)."
                if nombre
                else "⚠️ Esa skill ya no estaba pendiente.",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def on_dmn_propuesta(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """H9-c: el dueño aprueba (dpok) o descarta (dpno) una propuesta del DMN
        generativo (hipótesis, patrón). v1: aprobar = validar la idea; nunca auto-ejecuta."""
        q = update.callback_query
        if q is None or not q.data or self._pool is None:
            return
        await q.answer()
        if not self._es_admin(q.from_user.id if q.from_user else None):
            await q.edit_message_text("⛔ Solo el dueño decide sobre las propuestas.")
            return
        accion, _, pid_str = q.data.partition(":")
        try:
            pid = int(pid_str)
        except ValueError:
            return
        from for3s_core import dmn

        titulo = await dmn.resolver_propuesta(
            self._pool, pid, aprobar=(accion == "dpok"), por=q.from_user.id if q.from_user else None
        )
        if accion == "dpok":
            await q.edit_message_text(
                f"✅ Propuesta *{titulo or '?'}* aprobada (queda validada como idea)."
                if titulo
                else "⚠️ Esa propuesta ya no estaba pendiente.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await q.edit_message_text(
                f"🗑️ Propuesta *{titulo or '?'}* descartada."
                if titulo
                else "⚠️ Esa propuesta ya no estaba pendiente.",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def _correr_equipo_inner(
        self, msg, texto: str, scope_user_id=None, *, sesion=None, autor_id=None
    ) -> None:
        """Lanza el EQUIPO (S4) + sintetiza (S5) + responde, con PROGRESO EN VIVO
        (pulido H8 área A): un mensaje que se edita mostrando cada specialist
        (⏳→🔄→🟢/🔴) + línea final de gasto. DEFENSIVO: si algo falla, NO rompe."""
        from for3s_core import multiagente

        provider = getattr(self._agent, "_provider", None)
        fam = multiagente.decidir_familia(texto)
        fam_txt = "técnicos" if fam == "tecnica" else "generales"

        # estado del progreso: nombre → "⏳"|"🟢"|"🔴". Un solo mensaje editable.
        estado: dict[str, str] = {}
        prog = {"msg": None, "ultimo": ""}

        def _render() -> str:
            hechos = sum(1 for v in estado.values() if v != "⏳")
            cab = (
                f"🤝 <b>Equipo trabajando</b> ({hechos}/{len(estado)} listos) — "
                f"especialistas {fam_txt}\n\n"
            )
            lineas = []
            for nombre, bola in estado.items():
                etq = multiagente._ETIQUETAS.get(nombre, nombre)
                # A-i: 🔄 = en curso (matiz "en curso"), ⏳ = en cola, 🟢/🔴 = resuelto
                extra = " <i>(en curso)</i>" if bola == "🔄" else ""
                lineas.append(f"{bola} {etq}{extra}")
            return cab + "\n".join(lineas)

        async def _pintar() -> None:
            txt = _render()
            if txt == prog["ultimo"]:
                return
            prog["ultimo"] = txt
            try:
                if prog["msg"] is None:
                    prog["msg"] = await msg.reply_text(txt, parse_mode=ParseMode.HTML)
                else:
                    await prog["msg"].edit_text(txt, parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001 — progreso cosmético, nunca rompe
                pass

        async def on_progreso(evento) -> None:
            if evento["tipo"] == "inicio":
                for nombre in evento["nombres"]:
                    estado[nombre] = "⏳"  # en cola
            elif evento["tipo"] == "trabajando":
                estado[evento["nombre"]] = "🔄"  # A-i: en curso
            elif evento["tipo"] == "fin":
                estado[evento["nombre"]] = "🟢" if evento["ok"] else "🔴"
            await _pintar()

        # A-ii: "escribiendo…" de Telegram durante toda la corrida (señal de
        # actividad además del progreso). Se cancela en el finally pase lo que pase.
        typing = asyncio.create_task(_mantener_typing(msg.get_bot(), msg.chat_id))
        try:
            equipo = await multiagente.correr_equipo(
                texto, provider=provider, familia=fam, on_progreso=on_progreso
            )
            informe = await multiagente.sintetizar(equipo, provider=provider)
        finally:
            typing.cancel()

        # guardar el turno en memoria (con scope si es de un miembro)
        if self._pool is not None:
            try:
                await memory.record_turn(
                    self._pool,
                    sesion or self._owner_session,
                    role="assistant",
                    content=informe,
                    channel="telegram",
                    owner_user_id=scope_user_id,
                    telegram_user_id=autor_id,
                )
            except Exception:  # noqa: BLE001 — guardar memoria no debe romper la entrega
                logger.warning("no pude guardar el informe del equipo en memoria")

        # AI3 — AUDIT TRAIL DB-backed: el COORDINADOR registra la corrida + el
        # reporte de cada specialist (separación de escritura: el Hub escribe, los
        # specialists no). Defensivo: el audit NUNCA rompe la entrega del informe.
        if self._pool is not None:
            from for3s_core import handoff

            await handoff.registrar_corrida(
                self._pool,
                session_id=sesion or self._owner_session,
                telegram_user_id=autor_id,
                tarea=texto,
                equipo=equipo,
                informe=informe,
            )

        # A2 — línea de gasto: tiempo · tokens · cupo (el del dueño por ahora; con
        # el apartado H = 1 key por persona, mostrará el de cada usuario).
        toks = ""
        costo = getattr(equipo, "costo", None)
        if costo is not None:
            try:
                toks = f" · 🔢 ~{costo.total_tokens:,} tokens"
            except Exception:  # noqa: BLE001
                toks = ""
        cupo = format_cupo(*self._last_cupo)
        cupo_txt = f"\n{cupo}" if cupo else ""

        # A-iv: caso 0/N — TODOS los specialists fallaron. Mensaje honesto y
        # accionable en vez de un informe vacío/pobre.
        if equipo.n_ok == 0:
            aviso = (
                f"⚠️ <b>El equipo no pudo completar el análisis</b> "
                f"(los {len(equipo.reportes)} especialistas fallaron — "
                f"probablemente saturación temporal). Reintenta en un momento."
                f"{cupo_txt}"
            )
            try:
                if prog["msg"] is not None:
                    await prog["msg"].edit_text(aviso, parse_mode=ParseMode.HTML)
                else:
                    await msg.reply_text(aviso, parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                pass
            return

        resumen = (
            f"✅ <b>Equipo terminó</b> ({equipo.n_ok}/{len(equipo.reportes)} ok · "
            f"⏱ {equipo.segundos_total:.0f}s{toks}){cupo_txt}"
        )
        try:
            if prog["msg"] is not None:
                await prog["msg"].edit_text(resumen, parse_mode=ParseMode.HTML)
            else:
                await msg.reply_text(resumen, parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            pass

        # A-iii: encabezado limpio antes del informe (separa visualmente el
        # resumen del contenido). _enviar_html ya parte bien los mensajes largos.
        await _enviar_html(msg, f"📋 <b>Informe del equipo</b>\n\n{informe}")

    async def on_cupo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        user = update.effective_user
        if msg is None or user is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        # NO llama a Claude (cero tokens): muestra el último cupo conocido, que
        # vino GRATIS pegado a la última respuesta. Si aún no hay dato, lo dice.
        usage_5h, usage_7d = self._last_cupo
        cupo = format_cupo(usage_5h, usage_7d)
        if cupo:
            await msg.reply_text(cupo)
        else:
            await msg.reply_text(
                "🔋 Aún no tengo dato de cupo — mándame un mensaje y te lo muestro."
            )

    # ───────── comandos de ADMINISTRACIÓN (solo dueño/admin) ─────────

    def _es_admin(self, user_id: int | None) -> bool:
        """¿Puede usar comandos de admin? Hoy = el dueño. Base para rol admin
        futuro (multi-usuario): aquí se añadiría la lista de admins extra."""
        # TODO multi-usuario: admitir también IDs con rol 'admin' explícito.
        return self._owners.is_authorized(user_id)

    async def on_datos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/datos — PR3: analítica de uso (actividad, consumo de tokens, repos
        recurrentes, capacidades usadas, actividad por persona). Datos REALES, sin
        inflar (cada métrica verificada). Solo el dueño."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        if self._pool is None:
            await msg.reply_text("📊 Aún sin conexión a la BD.")
            return
        await msg.reply_text("📊 Calculando los datos de uso…")
        try:
            from for3s_core import analytics

            reporte = await analytics.reporte_datos(self._pool)
        except Exception as e:  # noqa: BLE001
            await msg.reply_text(f"📊 Error generando los datos: {type(e).__name__}")
            return
        for i in range(0, len(reporte), 3900):
            await msg.reply_text(reporte[i : i + 3900], parse_mode=ParseMode.MARKDOWN)

    async def on_ayuda(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/ayuda — PR10.1: qué es For3s, qué puede hacer, comandos SEGÚN EL ROL, y
        cómo resolver problemas comunes. Para TODOS (dueño y miembros). Es el primer
        auxilio para que el usuario no dependa de soporte humano."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        es_admin = self._es_admin(user.id)

        comunes = (
            "*❓ ¿Qué es For3s?*\n"
            "Tu segundo cerebro con memoria: conversa, recuerda lo que trabajan, lee "
            "repos de GitHub, archivos (PDF/imágenes/Word/Excel) y páginas web.\n\n"
            "*🗣️ Cómo usarme*\n"
            "• Solo escríbeme — recuerdo nuestra conversación entre sesiones.\n"
            "• Pregúntame *\"¿en qué quedamos?\"* para retomar.\n"
            "• Pásame un link de GitHub y lo analizo; un PDF/imagen y lo leo.\n\n"
            "*📋 Tus comandos*\n"
            "• /start — empezar · /ayuda — esto\n"
            "• /perfil — quién eres (rol, preferencias)\n"
            "• /tema · /temas · /hilos — organizar tus conversaciones por tema\n"
            "• /skills · /aprende — habilidades reutilizables\n"
            "• /version — versión y novedades · /estado — salud del agente\n"
            "• /cupo — cuánto queda de la suscripción\n"
        )
        admin = (
            "\n*🛠️ Comandos de dueño*\n"
            "• /salud — reporte completo de salud · /salud <sección>\n"
            "• /diagnostico — actividad reciente · /model — elegir modelo IA\n"
            "• /invitar — abrir/cerrar la puerta del equipo · /miembros\n"
            "• /dmn — el modo \"sueña\" · /autogen — auto-generar skills\n"
            "• /reiniciar — reconectar GitHub · /reiniciar_duro — reinicio total\n"
        )
        problemas = (
            "\n*🩺 ¿Algo no funciona?*\n"
            "• Si no respondo: la red del servidor puede estar inestable, reintenta en un momento.\n"
            "• Si no recuerdo algo: pregúntame distinto o pega el contexto; mi memoria es por tema.\n"
            "• Si GitHub/web falla: avísame e intento de nuevo (a veces el servicio externo se cae).\n"
        )
        if es_admin:
            problemas += "• Dueño: usa /salud para ver qué subsistema falla, y /reiniciar si es GitHub.\n"

        texto = comunes + (admin if es_admin else "") + problemas
        await msg.reply_text(texto, parse_mode=ParseMode.MARKDOWN)

    async def on_estado(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/estado — salud rápida del agente (cero tokens). Abierto a todos (BUG-12):
        es info no sensible (uptime/modelo) y estaba en el menú básico pero bloqueado."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        up = int(time.time() - self._started_at)
        h, m = up // 3600, (up % 3600) // 60
        mcp = "✅ conectado" if self._mcp is not None else "❌ no disponible"
        u5, u7 = self._last_cupo
        cupo = format_cupo(u5, u7) or "sin dato aún"
        await msg.reply_text(
            f"🤖 *Estado de For3s OS*\n"
            f"• Modelo: {self._model}\n"
            f"• GitHub MCP: {mcp}\n"
            f"• {cupo}\n"
            f"• Activo desde hace: {h}h {m}m",
        )

    async def on_salud(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/salud — reporte de salud END-TO-END del sistema (PR2): la línea
        mensaje→memoria, subsistemas, grafo, integraciones, ciclo nocturno, tokens
        por persona, hilos. Solo el dueño. Defensivo."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        if self._pool is None:
            await msg.reply_text("🩺 Aún sin conexión a la BD.")
            return
        # /salud → reporte completo · /salud <seccion> → solo esa (linea, tokens,
        # nocturno, grafo, integraciones, subsistemas, hilos) para no saturar.
        seccion = (context.args[0] if context.args else "").strip().lower()
        await msg.reply_text("🩺 Revisando la salud del sistema…")
        try:
            from for3s_core import health

            if seccion:
                reporte = await health.reporte_seccion(self._pool, seccion)
            else:
                reporte = await health.reporte_completo(self._pool)
        except Exception as e:  # noqa: BLE001 — el reporte nunca debe romper
            await msg.reply_text(f"🩺 Error generando el reporte: {type(e).__name__}")
            return
        for i in range(0, len(reporte), 3900):
            await msg.reply_text(reporte[i : i + 3900], parse_mode=ParseMode.MARKDOWN)

    async def on_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/skills — H10: lista las skills (recetas) de For3s. `/skills <nombre>` muestra
        una completa. El agente además las usa SOLO cuando aplican (match automático).
        La CREACIÓN de skills llega en H12 (/aprende). Para cualquier persona autorizada."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None or self._pool is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        from for3s_core.skills import SkillStore

        ss = SkillStore(self._pool)
        args = context.args or []
        if args:  # ver una skill concreta
            sk = await ss.ver(" ".join(args))
            if not sk:
                await msg.reply_text("🧩 No encontré esa skill. Usa /skills para ver la lista.")
                return
            await ss.registrar_uso(sk["id"])
            await _enviar_html(
                msg, f"🧩 <b>{sk['nombre']}</b> ({sk['categoria']})\n\n" + sk["contenido"][:3500]
            )
            return
        # listar
        lista = await ss.listar()
        if not lista:
            await msg.reply_text(
                "🧩 Aún no tengo skills guardadas. Pronto podré aprenderlas (H12 /aprende)."
            )
            return
        lineas = [f"🧩 *Skills de For3s* ({len(lista)}):", ""]
        for sk in lista:
            prov = "🤖" if sk.provenance == "auto" else "👤"
            lineas.append(f"{prov} `{sk.nombre}` ({sk.categoria}) — {sk.descripcion or 's/desc'}")
        lineas.append("\n_Ve una con `/skills <nombre>`. Las uso solas cuando aplican._")
        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_perfil(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/perfil — P1: cada persona ve/edita SU perfil (rol, stack, estilo, zona).
        Sin args → muestra el perfil. Con args `/perfil <campo> <valor>` → lo fija
        (ej. '/perfil rol backend'). El bot usa el perfil para adaptar sus respuestas."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None or self._pool is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        from for3s_core.perfil import PerfilStore

        ps = PerfilStore(self._pool)
        args = context.args or []
        CAMPOS = ("rol", "stack", "estilo", "zona", "nombre")
        # editar: /perfil <campo> <valor>
        if len(args) >= 2 and args[0].lower() in CAMPOS:
            campo = args[0].lower()
            valor = " ".join(args[1:]).strip()[:120]
            await ps.set_campo(user.id, campo, valor, nombre=getattr(user, "full_name", None))
            await msg.reply_text(
                f"✅ Perfil actualizado: *{campo}* = {valor}", parse_mode=ParseMode.MARKDOWN
            )
            return
        # mostrar el perfil
        p = await ps.get(user.id)
        if not p or not any(p.get(c) for c in CAMPOS) and not p.get("rasgos"):
            await msg.reply_text(
                '👤 Aún no tengo tu perfil. Cuéntame de ti (ej. "soy backend", '
                '"prefiero respuestas cortas") o usa `/perfil rol <tu rol>`.\n\n'
                "Campos: rol · stack · estilo · zona.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        lineas = ["👤 *Tu perfil* (lo uso para adaptar mis respuestas):", ""]
        for c in CAMPOS:
            if p.get(c):
                lineas.append(f"• {c}: {p[c]}")
        for r in p.get("rasgos") or []:
            lineas.append(f"• {r}")
        lineas.append("\n_Edita con `/perfil <campo> <valor>` o dímelo en el chat._")
        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_aprende(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/aprende [tema] — H12 P1: destila una skill (receta) de la conversación
        actual y la guarda. La pidió un humano → provenance='usuario' (directa), pero
        SIEMPRE pasa por el scanner del governor (H11). Para cualquier persona
        autorizada (también miembros: pueden enseñar recetas de su trabajo)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        if self._pool is None or self._agent is None:
            await msg.reply_text("⚠️ No puedo aprender ahora (sistema no listo).")
            return
        provider = getattr(self._agent, "_provider", None)
        if provider is None:
            await msg.reply_text("⚠️ No tengo el cerebro disponible para destilar.")
            return
        foco = " ".join(context.args).strip() if context.args else ""
        await msg.reply_text("🧠 Destilando una skill de lo que trabajamos…")
        try:
            from for3s_core.aprende import aprender_de_conversacion

            sesion = await self._sesion_de(user)
            res = await aprender_de_conversacion(
                self._pool, provider, sesion, creada_por=user.id, foco=foco
            )
        except Exception:  # noqa: BLE001 — aprender nunca debe tumbar el bot
            logger.warning("on_aprende falló", exc_info=True)
            await msg.reply_text("⚠️ Algo falló al destilar la skill. Inténtalo de nuevo.")
            return
        if res.ok:
            await msg.reply_text(
                f"✅ {res.mensaje}\nLa usaré cuando aplique. Mírala con `/skills {res.nombre}`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await msg.reply_text(f"ℹ️ {res.mensaje}")

    async def on_autogen(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/autogen on|off|status — KILL SWITCH de la auto-generación de skills (H11).

        Solo el dueño. El governor (H11) es el freno; este comando es su interruptor.
        Default del sistema: auto-gen APAGADA (no se genera nada hasta H12 + tu visto
        bueno). `status` muestra la salud del ecosistema (sin tokens LLM)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Solo el dueño controla la auto-generación.")
            return
        if self._pool is None:
            await msg.reply_text("⚠️ Base de datos no disponible.")
            return
        from for3s_core.governor import (
            MAX_ACTIVE_SKILLS,
            MAX_NEW_SKILLS_AUTO_PER_DAY,
            SkillEcosystemGovernor,
        )

        gov = SkillEcosystemGovernor(self._pool)
        arg = context.args[0].strip().lower() if context.args else "status"

        if arg in ("on", "off"):
            await gov.set_autogen(arg == "on", por=user.id, motivo=f"/autogen {arg} por el dueño")
            estado = "🟢 ENCENDIDA" if arg == "on" else "🔴 APAGADA"
            extra = (
                "\n\n⚠️ Aún no hay motor (H12): nada se auto-genera todavía, "
                "pero el freno queda listo."
                if arg == "on"
                else "\n\nLa auto-generación queda congelada. Tus skills y las creadas "
                "a mano siguen intactas."
            )
            await msg.reply_text(f"Auto-generación de skills: {estado}.{extra}")
            return

        # status (default): reporte de salud del ecosistema
        h = await gov.health_report()
        emoji = {"HEALTHY": "🟢", "THROTTLED": "🟡", "FROZEN": "🔴"}.get(h.veredicto, "⚪")
        lineas = [
            f"🛡️ *Governor de skills* — {emoji} {h.veredicto}",
            "",
            f"• Auto-generación: {'🟢 ON' if h.autogen_on else '🔴 OFF (kill switch)'}",
            f"• Skills activas: {h.active_skills}/{MAX_ACTIVE_SKILLS}",
            f"• Auto-creadas hoy: {h.new_skills_auto_today}/{MAX_NEW_SKILLS_AUTO_PER_DAY}",
            f"• Bloqueos del governor hoy: {h.bloqueos_today}",
            "",
            "_El governor es el freno: escanea toda skill nueva (patrones peligrosos) "
            "y limita la auto-generación. `/autogen on|off` controla el interruptor._",
        ]
        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_dmn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/dmn status | housekeeping on|off | generativas on|off | correr — H9 SUEÑA.

        El DMN trabaja solo cuando el sistema está idle. Solo el dueño. Default:
        housekeeping ON (se mantiene solo), generativas OFF (se mejora solo — se
        encienden tras calibrar). `correr` fuerza un ciclo ahora (para probar)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Solo el dueño controla el DMN.")
            return
        if self._pool is None:
            await msg.reply_text("⚠️ Base de datos no disponible.")
            return
        from for3s_core import (
            dmn,
            dmn_tasks,  # noqa: F401 — registra las housekeeping
        )

        args = [a.strip().lower() for a in (context.args or [])]
        sub = args[0] if args else "status"

        # encender/apagar una clase: /dmn housekeeping on  ·  /dmn generativas off
        if sub in ("housekeeping", "generativas") and len(args) >= 2 and args[1] in ("on", "off"):
            clase = dmn.CLASE_HOUSEKEEPING if sub == "housekeeping" else dmn.CLASE_GENERATIVA
            await dmn.set_clase(
                self._pool,
                clase,
                args[1] == "on",
                por=user.id,
                motivo=f"/dmn {sub} {args[1]} por el dueño",
            )
            await msg.reply_text(f"DMN · {sub}: {'🟢 ON' if args[1] == 'on' else '🔴 OFF'}.")
            return

        # forzar un ciclo ahora (ignora idle) — para probar
        if sub == "correr":
            await msg.reply_text("🌙 Corriendo un ciclo del DMN…")
            rep = await dmn.correr_ciclo(self._pool, solo_noche=True, forzar=True)
            await msg.reply_text(f"✅ {rep}")
            return

        # ver/decidir las propuestas generativas pendientes
        if sub == "propuestas":
            props = await dmn.propuestas_pendientes(self._pool)
            if not props:
                await msg.reply_text("🌙 No hay propuestas del DMN pendientes.")
                return
            for pr in props:
                teclado = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ Aprobar", callback_data=f"dpok:{pr.id}"),
                            InlineKeyboardButton("❌ Descartar", callback_data=f"dpno:{pr.id}"),
                        ]
                    ]
                )
                await msg.reply_text(
                    f"💡 *{pr.tipo}* — {pr.titulo}\n\n{pr.contenido[:600]}\n\n_(de {pr.task})_",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=teclado,
                )
            return

        # ROI por task (cada task se gana su lugar — H9-d)
        if sub == "roi":
            rois = await dmn.roi_por_task(self._pool)
            if not rois:
                await msg.reply_text("🌙 Aún no hay corridas del DMN para medir ROI.")
                return
            emoji = {"keep": "🟢", "revisar": "🟡", "sin-datos": "⚪"}
            lineas = ["📊 *ROI del DMN* (últimos 30 días)", ""]
            for r in rois:
                lineas.append(
                    f"{emoji.get(r.recomendacion, '⚪')} `{r.task}` — corrió {r.corridas}× · "
                    f"${r.costo_total:.2f} · {r.recomendacion}"
                )
            lineas.append("\n_🟡 revisar = gastó pero casi no produjo (candidata a apagar)._")
            await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)
            return

        # status (default)
        st = await dmn.status(self._pool)
        idle = f"{st.idle_min:.0f} min" if st.idle_min is not None else "—"
        lineas = [
            "🌙 *DMN — SUEÑA* (trabaja solo cuando estás inactivo)",
            "",
            f"• Inactividad actual: {idle}  (despierta a los {dmn.IDLE_MIN} min)",
            f"• Housekeeping (se mantiene): {'🟢 ON' if st.housekeeping_on else '🔴 OFF'}",
            f"• Generativas (se mejora): {'🟢 ON' if st.generativas_on else '🔴 OFF'}",
            f"• Tasks registradas: {st.tasks_registradas}",
            f"• Corridas hoy: {st.corridas_hoy}",
            "",
            "_`/dmn housekeeping on|off` · `/dmn generativas on|off` · `/dmn correr` · "
            "`/dmn propuestas` · `/dmn roi`._",
        ]
        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_version(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/version — versión + hito + lo más nuevo del agente (AI5, cero tokens LLM).
        Para cualquier persona autorizada."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        from for3s_core import version as _ver

        sv = None
        if self._pool is not None:
            try:
                sv = await self._pool.fetchval("SELECT max(version) FROM schema_version")
            except Exception:  # noqa: BLE001 — schema_version opcional
                sv = None
        nuevo = _ver.CHANGELOG[0]
        lineas = [
            f"📋 *For3s OS v{_ver.VERSION}* — {_ver.HITO}",
            f"_{_ver.HITO_DESC}_",
            "",
            f"*Lo más nuevo* ({nuevo['fecha']}):",
        ]
        lineas += [f"• {c}" for c in nuevo["cambios"]]
        lineas.append("")
        lineas.append(
            "Hitos: H1-H4 (MVP) · H5 memoria · H6 se cuida · H7 /model · "
            "H8 equipo · H9 sueña · H10-12 aprende · metacognición (planea)"
        )
        if sv is not None:
            lineas.append(f"_(esquema BD interno: v{sv})_")
        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/model — lista los modelos del catálogo y deja elegir (estilo Claude Code).
        Solo el dueño. El enrutamiento automático (H7) está bloqueado: esto es la
        selección MANUAL del modelo que usa el bot."""
        from for3s_core import modelos

        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        actual = await modelos.get_seleccionado(self._pool, self._owner_session)
        botones = []
        for m in modelos.CATALOGO:
            check = " ✓" if m.id == actual else ""
            botones.append(
                [
                    InlineKeyboardButton(
                        f"{m.nombre}{check} — {m.desc}", callback_data=f"model:{m.id}"
                    )
                ]
            )
        await msg.reply_text(
            "🧠 *Selecciona el modelo de For3s OS*\n(el que se usa para responder; ✓ = activo)",
            reply_markup=InlineKeyboardMarkup(botones),
        )

    async def on_model_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Botón de /model: guarda la selección y la aplica en caliente al provider."""
        from for3s_core import modelos

        q = update.callback_query
        if q is None or not q.data:
            return
        await q.answer()
        user_id = q.from_user.id if q.from_user else None
        if not self._es_admin(user_id):
            await q.edit_message_text("⛔ Solo el dueño puede cambiar el modelo.")
            return
        _, _, model_id = q.data.partition(":")
        info = modelos.info_de(model_id)
        if not info:
            await q.edit_message_text("❌ Modelo no reconocido.")
            return
        ok = await modelos.set_seleccionado(self._pool, self._owner_session, model_id)
        if ok and self._agent is not None:
            # aplicar en caliente: el provider cambia de modelo sin reiniciar
            prov = getattr(self._agent, "_provider", None)
            if prov is not None and hasattr(prov, "set_model"):
                prov.set_model(model_id)
            self._model = model_id
        await audit.append(
            self._pool,
            actor="user",
            action="model_changed",
            detail={"model": model_id},
        )
        await q.edit_message_text(f"✅ Modelo cambiado a *{info.nombre}* ({info.id}).")

    # ───────── H8 S10e: /invitar (la PUERTA) + gate de aprobación ─────────

    async def on_invitar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/invitar — la PUERTA del equipo (modelo del dueño). Solo el encargado/dueño.
        Muestra el estado actual y botones para abrir/cerrar. Abrir = quien escriba
        al bot entra al equipo; cerrar = solo los de adentro siguen. Crea el equipo
        la primera vez (rollout silencioso: hasta aquí el bot era single-owner)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None or self._equipo is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Solo el encargado puede abrir o cerrar la puerta.")
            return
        eid = await self._equipo.asegurar_equipo(
            user.id, nombre_encargado=getattr(user, "full_name", None)
        )
        abierta = await self._equipo.puerta_abierta(eid)
        estado = (
            "🟢 ABIERTA — quien me escriba entra al equipo"
            if abierta
            else "🔴 CERRADA — solo el dueño y los que ya están dentro"
        )
        n = len(await self._equipo.miembros(eid))
        botones = [
            [
                InlineKeyboardButton("🟢 Abrir puerta", callback_data="puerta:abrir"),
                InlineKeyboardButton("🔴 Cerrar puerta", callback_data="puerta:cerrar"),
            ]
        ]
        await msg.reply_text(
            f"🚪 *Puerta del equipo*\n\nEstado: {estado}\nMiembros: {n}\n\n"
            "Abre la puerta, pide a tu gente que me escriba, y ciérrala cuando "
            "todos hayan entrado.",
            reply_markup=InlineKeyboardMarkup(botones),
        )

    async def on_puerta_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Botón de /invitar: abre o cierra la puerta. Solo el encargado/dueño."""
        q = update.callback_query
        if q is None or not q.data or self._equipo is None:
            return
        await q.answer()
        uid = q.from_user.id if q.from_user else None
        if not self._es_admin(uid):
            await q.edit_message_text("⛔ Solo el encargado puede tocar la puerta.")
            return
        _, _, accion = q.data.partition(":")
        nombre_enc = q.from_user.full_name if q.from_user else None
        eid = await self._equipo.asegurar_equipo(uid, nombre_encargado=nombre_enc)
        abrir = accion == "abrir"
        await self._equipo.set_puerta(eid, abrir)
        await audit.append(
            self._pool,
            actor="user",
            action="puerta_equipo",
            detail={"abierta": abrir, "equipo_id": eid},
        )
        if abrir:
            await q.edit_message_text(
                "🟢 *Puerta ABIERTA.* Quien me escriba ahora entra al equipo.\n"
                "Cuando ya estén todos, vuelve a /invitar y ciérrala."
            )
        else:
            await q.edit_message_text(
                "🔴 *Puerta CERRADA.* Ya nadie nuevo puede entrar; los que están "
                "dentro siguen con acceso."
            )

    async def on_gate_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Botones ✅/❌ del gate (E): el encargado aprueba/rechaza una acción
        sensible (write GitHub) que un MIEMBRO propuso. Al APROBAR, EJECUTA la write
        real con el payload guardado (PAT del dueño por ahora; con BYOK usará la del
        miembro) y avisa al solicitante. Al rechazar, avisa que se rechazó."""
        q = update.callback_query
        if q is None or not q.data or self._equipo is None:
            return
        await q.answer()
        uid = q.from_user.id if q.from_user else None
        accion, _, sid_str = q.data.partition(":")
        try:
            sid = int(sid_str)
        except ValueError:
            return

        if accion == "gateok":
            sol = await self._equipo.aprobar(sid, uid)
            if sol is None:
                await q.edit_message_text("⚠️ No pude aprobar (ya resuelta o no eres el encargado).")
                return
            # E — EJECUTAR la write real desde el payload de la solicitud.
            payload = sol.payload or {}
            name = payload.get("tool")
            args = payload.get("args", {})
            solicitante = payload.get("solicitante")
            if name not in WRITE_TOOLS_PERMITIDAS or self._pat is None:
                await q.edit_message_text(
                    f"✅ Aprobaste: {sol.descripcion}\n"
                    "⚠️ Pero no pude ejecutar (acción no permitida o GitHub no disponible)."
                )
                return
            await q.edit_message_text("⏳ Aprobado — ejecutando en GitHub…")
            try:
                resultado = await ejecutar_write(self._pat, name, args)
                ok = True
            except Exception as exc:  # noqa: BLE001
                logger.exception("falló ejecución de write aprobada %s", name)
                resultado = f"{type(exc).__name__}: {exc}"
                ok = False
            await audit.append(
                self._pool,
                actor="user",
                action="github_write_gate",
                detail={
                    "tool": name,
                    "args": args,
                    "ok": ok,
                    "aprobado_por": uid,
                    "solicitante": solicitante,
                    "result": str(resultado)[:1000],
                },
            )
            if ok:
                await q.edit_message_text(
                    f"✅ Aprobado y ejecutado en GitHub.\n\n"
                    f"<code>{str(resultado or '')[:500]}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await q.edit_message_text(
                    f"✅ Aprobaste, pero la ejecución falló: {str(resultado)[:300]}"
                )
            # avisar al MIEMBRO solicitante (proactivo, defensivo)
            if solicitante:
                try:
                    txt = (
                        "✅ El encargado aprobó tu acción y se ejecutó en GitHub."
                        if ok
                        else "⚠️ El encargado la aprobó pero la ejecución falló."
                    )
                    await context.bot.send_message(chat_id=solicitante, text=txt)
                except Exception:  # noqa: BLE001
                    pass
        else:  # gateno
            sol = await self._equipo.rechazar(sid, uid)
            if sol is None:
                await q.edit_message_text(
                    "⚠️ No pude rechazar (ya resuelta o no eres el encargado)."
                )
                return
            await q.edit_message_text(f"❌ Rechazaste: {sol.descripcion}")
            # avisar al miembro que fue rechazada
            solicitante = (sol.payload or {}).get("solicitante")
            if solicitante:
                try:
                    await context.bot.send_message(
                        chat_id=solicitante,
                        text="❌ El encargado no aprobó tu acción de escritura.",
                    )
                except Exception:  # noqa: BLE001
                    pass

    # ───────── AI2: /tema y /temas (shared-thread inbox por persona) ─────────

    async def on_tema(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/tema [nombre] — crea/cambia el tema activo de QUIEN escribe. Sin nombre
        → muestra el activo. Cada tema es un hilo de conversación separado."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None or self._temas is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        arg = " ".join(context.args).strip() if context.args else ""
        if not arg:
            activo = await self._temas.activo(user.id)
            await msg.reply_text(
                f"📁 Tu tema activo: *{activo}*\n\nUsa `/tema <nombre>` para crear o "
                "cambiar de tema, o /temas para ver los tuyos.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        slug = await self._temas.cambiar(user.id, arg)
        await msg.reply_text(
            f"📁 Tema activo: *{slug}*\nA partir de ahora hablamos en este hilo, "
            "separado de los demás. (Tu memoria/conocimiento se sigue compartiendo.)",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def on_temas(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/temas — lista los temas de QUIEN escribe, con botones para cambiar."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None or self._temas is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        lista = await self._temas.listar(user.id)
        if not lista:
            await msg.reply_text(
                "📁 Aún no tienes temas (estás en *general*).\nCrea uno con "
                "`/tema <nombre>` — ej. `/tema backend`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        botones = [
            [InlineKeyboardButton(f"📁 {n}{' ✓' if a else ''}", callback_data=f"tema:{n}")]
            for n, a in lista
        ]
        await msg.reply_text(
            "📁 *Tus temas* (✓ = activo). Toca uno para cambiar:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def on_tema_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Botón de /temas: cambia al tema elegido (de quien toca el botón)."""
        q = update.callback_query
        if q is None or not q.data or self._temas is None:
            return
        await q.answer()
        uid = q.from_user.id if q.from_user else None
        if uid is None:
            return
        ok, _ = await self._autorizar(q.from_user)
        if not ok:
            await q.edit_message_text("⛔ Este bot es privado.")
            return
        _, _, nombre = q.data.partition(":")
        slug = await self._temas.cambiar(uid, nombre)
        await q.edit_message_text(
            f"📁 Tema activo: *{slug}*\nHablamos en este hilo de ahora en adelante.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def on_equipo_sugerido(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """B (pulido H8): botones de la sugerencia de equipo. 🤝 → lanza el equipo
        con el texto original; 💬 → responde el agente normal. Solo quien hizo la
        petición (y autorizado) puede decidir."""
        q = update.callback_query
        if q is None or not q.data:
            return
        await q.answer()
        accion, _, sid_sug = q.data.partition(":")
        pend = self._equipo_sugerido.pop(sid_sug, None)
        uid = q.from_user.id if q.from_user else None
        if pend is None:
            await q.edit_message_text("⏳ Esa sugerencia ya expiró. Pídelo de nuevo si quieres.")
            return
        if uid != pend["user_id"]:
            await q.edit_message_text("⛔ Solo quien lo pidió puede decidir.")
            return
        texto = pend["texto"]
        if accion == "eqno":
            # responder el agente normal con el texto original (reusa on_message)
            await q.edit_message_text("💬 Va, te respondo yo solo…")
            await self._responder_agente_simple(q, texto, q.from_user)
            return
        # eqsi → lanzar el equipo
        await q.edit_message_text("🤝 Lanzando el equipo…")
        scope = None if self._owners.is_authorized(uid) else uid
        try:
            await self._correr_equipo_y_responder(
                q.message,
                texto,
                scope_user_id=scope,
                sesion=await self._sesion_de(q.from_user),
                autor_id=uid,
            )
        except Exception:  # noqa: BLE001 — el equipo no debe tumbar el bot
            logger.warning("equipo (sugerido) falló", exc_info=True)
            await self._responder_agente_simple(q, texto, q.from_user)

    async def _responder_agente_simple(self, q, texto: str, user) -> None:
        """Responde con el agente de 1 (camino normal) desde un callback. Usado
        cuando el usuario elige '💬 responde tú solo' en la sugerencia de equipo."""
        try:
            sesion = await self._sesion_de(user)
            convo = Conversation(
                self._pool,
                self._agent,
                sesion,
                channel="telegram",
                telegram_user_id=user.id,
                scope_user_id=self._scope_de(user),
            )
            ctx_t = tiempo.contexto_temporal(getattr(user, "language_code", None))
            resp = await asyncio.wait_for(
                convo.send(texto, max_tokens=2048, contexto=ctx_t), timeout=ANALYSIS_TIMEOUT
            )
            await _enviar_html(q.message, resp.text)
        except Exception:  # noqa: BLE001
            logger.exception("fallo respondiendo (agente simple desde sugerencia)")
            await _responder_seguro(q.message, "❌ Algo falló. Reenvía tu mensaje, por favor.")

    async def on_kick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """C-v: botón [🚫 Sacar] de /miembros → pide CONFIRMACIÓN. Solo el encargado."""
        q = update.callback_query
        if q is None or not q.data or self._equipo is None:
            return
        await q.answer()
        uid = q.from_user.id if q.from_user else None
        if not self._es_admin(uid):
            await q.edit_message_text("⛔ Solo el encargado puede sacar miembros.")
            return
        _, _, obj_str = q.data.partition(":")
        try:
            objetivo = int(obj_str)
        except ValueError:
            return
        eid = await self._equipo.equipo_de(uid)
        if eid is None:
            await q.edit_message_text("⚠️ No encontré tu equipo.")
            return
        # nombre del objetivo (para la confirmación)
        nombre = "ese miembro"
        for m in await self._equipo.miembros(eid):
            if m.user_id == objetivo:
                nombre = m.nombre or "ese miembro"
                break
        teclado = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Sí, sacar", callback_data=f"kok:{objetivo}"),
                    InlineKeyboardButton("❌ Cancelar", callback_data="kno:0"),
                ]
            ]
        )
        await q.edit_message_text(
            f"⚠️ ¿Seguro que quieres sacar a *{nombre}* del equipo?\n"
            "Perderá el acceso (su historial se conserva). No re-entra por la puerta "
            "abierta; solo si lo vuelves a invitar.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=teclado,
        )

    async def on_kick_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """C-v: confirma o cancela el kick. Ejecuta sacar_miembro (verifica en BD)."""
        q = update.callback_query
        if q is None or not q.data or self._equipo is None:
            return
        await q.answer()
        uid = q.from_user.id if q.from_user else None
        accion, _, obj_str = q.data.partition(":")
        if accion == "kno":
            await q.edit_message_text("❌ Cancelado. Nadie fue sacado.")
            return
        if not self._es_admin(uid):
            await q.edit_message_text("⛔ Solo el encargado puede sacar miembros.")
            return
        try:
            objetivo = int(obj_str)
        except ValueError:
            return
        eid = await self._equipo.equipo_de(uid)
        if eid is None:
            await q.edit_message_text("⚠️ No encontré tu equipo.")
            return
        ok, motivo = await self._equipo.sacar_miembro(eid, uid, objetivo)
        if ok:
            await audit.append(
                self._pool,
                actor="user",
                action="miembro_sacado",
                detail={"equipo_id": eid, "objetivo": objetivo},
            )
            await q.edit_message_text("✅ Listo. Esa persona ya no tiene acceso al equipo.")
            # avisar al sacado (proactivo, defensivo)
            try:
                await context.bot.send_message(
                    chat_id=objetivo,
                    text="ℹ️ El encargado te quitó el acceso a este equipo. "
                    "Si crees que fue un error, contáctalo.",
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            avisos = {
                "no_puedes_sacarte": "No puedes sacarte a ti mismo.",
                "no_eres_encargado": "Solo el encargado puede sacar.",
                "no_es_miembro": "Esa persona ya no está en el equipo.",
                "no_puedes_sacar_encargado": "No puedes sacar a otro encargado.",
            }
            await q.edit_message_text(f"⚠️ {avisos.get(motivo, 'No se pudo sacar.')}")

    # ───────── AI7: /miembros (encargado) + /hilos (cada persona) ─────────

    async def on_miembros(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/miembros — el ENCARGADO ve quién está en el equipo (nombre, rol). Responde
        la observación de Nota: 'cómo sé quién entró'. Si no hay equipo (single-owner),
        lo dice y sugiere /invitar."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None or self._equipo is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Solo el encargado puede ver el equipo.")
            return
        eid = await self._equipo.equipo_de(user.id)
        if eid is None:
            await msg.reply_text(
                "👥 Aún no has armado equipo — eres el único usuario.\n"
                "Usa /invitar para abrir la puerta y que entren otras personas."
            )
            return
        miembros = await self._equipo.miembros(eid)
        abierta = await self._equipo.puerta_abierta(eid)
        puerta = "🟢 abierta" if abierta else "🔴 cerrada"
        lineas = [f"👥 *Equipo* ({len(miembros)} miembro(s)) · puerta {puerta}", ""]
        botones = []
        for m in miembros:
            icono = "👑" if m.rol == equipo_mod.ROL_ENCARGADO else "👤"
            nombre = m.nombre or "(sin nombre)"
            # M3: última actividad real (health) — 'activo hoy', 'hace 3 días'...
            act = _humanizar_fecha(m.ultima_actividad) if m.ultima_actividad else "sin actividad"
            lineas.append(f"{icono} {nombre} — {m.rol} · {act}")
            # C-v: botón [🚫 Sacar] por cada MIEMBRO (no el encargado, no se autosaca)
            if m.rol != equipo_mod.ROL_ENCARGADO:
                botones.append(
                    [
                        InlineKeyboardButton(
                            f"🚫 Sacar a {nombre}", callback_data=f"kick:{m.user_id}"
                        )
                    ]
                )
        teclado = InlineKeyboardMarkup(botones) if botones else None
        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN, reply_markup=teclado)

    async def on_hilos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/hilos — cada persona ve SUS hilos (temas) + su actividad. Complementa
        /temas (que sirve para CAMBIAR); este es la VISTA con actividad real."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None or self._temas is None:
            return
        ok, _ = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        base = self._base_sesion(user)
        hilos = await self._temas.resumen_hilos(user.id, base)
        lineas = ["📁 *Tus hilos* (cada uno es una conversación separada):", ""]
        for h in hilos:
            marca = "🟢" if h.activo else "📁"
            cuando = _humanizar_fecha(h.ultimo_uso)
            extra = f" · {h.turnos} mensajes · {cuando}" if h.turnos else " · vacío"
            activo_txt = " (activo)" if h.activo else ""
            lineas.append(f"{marca} {h.nombre}{activo_txt}{extra}")
        lineas.append("")
        lineas.append("_Cambia con /tema <nombre> o /temas._")
        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_diagnostico(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/diagnostico — PR10.2: auto-diagnóstico PERSONAL para CUALQUIER usuario.
        Cada quien ve SU situación (rol, su hilo, su memoria, su perfil) — NUNCA la de
        otro. ⭐ FIX BUG-13: antes leía siempre la sesión 'brian' del dueño (fuga); ahora
        usa _sesion_de(user) (la sesión real de quien pregunta, respeta H8/AI1)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        # autorización aditiva (dueño/miembro/puerta) — mismo gate que el resto
        ok, motivo = await self._autorizar(user)
        if not ok:
            await msg.reply_text("⛔ Este bot es privado.")
            return
        if self._pool is None:
            await msg.reply_text("🩺 Aún sin conexión a la BD.")
            return
        from for3s_core import memory

        es_admin = self._es_admin(user.id)
        rol = "👑 dueño" if es_admin else "👤 miembro"
        # la SESIÓN REAL del usuario (su hilo×tema), NO la fija del dueño (FIX BUG-13)
        sesion = await self._sesion_de(user)

        lineas = ["🩺 *Tu diagnóstico*", f"• Te reconozco como: {rol}"]

        # ¿el bot te reconoce bien? (la prueba que destapó el bug de la migración)
        if es_admin and self._owners.get_owner() != user.id:
            lineas.append("⚠️ OJO: eres admin pero el owner_id no coincide (revisar)")

        # TU memoria en TU hilo actual (no la de otro)
        try:
            turns = await memory.load_history(self._pool, sesion, last_n=4)
            lineas.append(f"• Tu hilo actual: `{sesion}` ({len(turns)} turnos recientes)")
            for tr in turns[-3:]:
                quien = "👤" if tr.role == "user" else "🤖"
                lineas.append(f"  {quien} {tr.content[:55].replace(chr(10), ' ')}")
        except Exception:  # noqa: BLE001
            lineas.append("• Tu memoria: no pude leerla ahora")

        # TU perfil (si lo tienes)
        try:
            from for3s_core.perfil import PerfilStore

            perfil = await PerfilStore(self._pool).get(user.id)
            lineas.append(f"• Tu perfil: {'configurado' if perfil else 'sin configurar (usa /perfil)'}")
        except Exception:  # noqa: BLE001
            pass

        # TUS hilos/temas
        try:
            if self._temas is not None:
                hilos = await self._temas.resumen_hilos(user.id, self._base_sesion(user))
                lineas.append(f"• Tus temas/hilos: {len(hilos)}")
        except Exception:  # noqa: BLE001
            pass

        # HA-1b: qué analizó el EQUIPO en TU hilo (cablea handoff.ultimas_corridas,
        # que estaba huérfana). Solo metadatos de TU sesión → respeta el aislamiento.
        try:
            from for3s_core import handoff

            sesion = await self._sesion_de(user)
            corridas = await handoff.ultimas_corridas(self._pool, sesion, limite=3)
            if corridas:
                lineas.append(f"• Equipo en tu hilo: {len(corridas)} corrida(s) reciente(s)")
                for c in corridas[:3]:
                    tarea = (c.get("tarea") or "")[:40]
                    lineas.append(f"   ↳ {tarea} ({c.get('n_ok')}/{c.get('n_specialists')} ok)")
        except Exception:  # noqa: BLE001 — sección secundaria, nunca rompe el diagnóstico
            pass

        # estado de servicios (info no sensible, útil para saber si algo está caído)
        mcp = "✅" if self._mcp is not None else "❌ (avísame para reconectar)"
        lineas.append(f"\n*Servicios:* GitHub {mcp} · modelo {self._model}")
        if es_admin:
            lineas.append("Para salud completa del sistema usa /salud.")
        else:
            lineas.append("Si algo no funciona, escríbeme o usa /ayuda.")

        await msg.reply_text("\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_transferir(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/transferir_dueno <user_id> — PR6.2a: transferir el control del bot a otra
        persona. SOLO el dueño. Pide CONFIRMACIÓN (regala el control → máximo cuidado).
        La transferencia es atómica (owner + encargado del equipo + JSON)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        # BUG-17 (2026-06-30): DESHABILITADO temporalmente. La transferencia NO renombra
        # las sesiones de memoria → el nuevo dueño heredaría la sesión 'brian' con TODO el
        # historial PRIVADO del dueño anterior (fuga), y el anterior perdería el suyo. El
        # fix de raíz (desacoplar identidad-de-sesión del workspace-de-cifrado, migrar 7
        # tablas + sessions con orden de FKs) está en PENDIENTES §AUDITORÍA CRÍTICA. Hasta
        # entonces se bloquea para que NADIE dispare la fuga.
        await msg.reply_text(
            "⚠️ La transferencia de dueño está *temporalmente deshabilitada* mientras "
            "rediseñamos cómo se mueve la memoria (para no exponer tu historial privado "
            "al nuevo dueño). Se reactivará pronto. — BUG-17",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
        # parsear el user_id destino
        if not context.args or not context.args[0].lstrip("-").isdigit():
            await msg.reply_text(
                "Uso: /transferir_dueno <user_id de Telegram>\n"
                "⚠️ Le das el CONTROL TOTAL del bot a esa persona (tú dejas de ser dueño)."
            )
            return
        nuevo = int(context.args[0])
        if nuevo == user.id:
            await msg.reply_text("Ya eres el dueño. 🙂")
            return
        # guardar pendiente + pedir confirmacion (doble check)
        self._transfer_pendiente = nuevo
        teclado = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Sí, transferir", callback_data=f"transf:{nuevo}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="transf:no"),
        ]])
        await msg.reply_text(
            f"⚠️ *Transferir el control del bot al usuario {nuevo}?*\n\n"
            "Esto le da CONTROL TOTAL (admin) y TÚ DEJAS DE SER DUEÑO. No se puede "
            "deshacer salvo que el nuevo dueño te lo transfiera de vuelta. ¿Seguro?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=teclado,
        )

    async def on_transferir_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Callback de confirmación de /transferir_dueno (botón ✅/❌). PR6.2a."""
        q = update.callback_query
        if q is None:
            return
        await q.answer()
        # solo el dueño actual puede confirmar
        if not self._es_admin(q.from_user.id if q.from_user else None):
            await q.edit_message_text("⛔ Solo el dueño puede confirmar esto.")
            return
        dato = (q.data or "").split(":", 1)[1] if ":" in (q.data or "") else "no"
        if dato == "no":
            self._transfer_pendiente = None
            await q.edit_message_text("❌ Transferencia cancelada. Sigues siendo el dueño.")
            return
        nuevo = int(dato)
        ok, motivo = await self._owners.transferir(self._pool, nuevo)
        if ok:
            await q.edit_message_text(
                f"✅ Listo. El usuario {nuevo} es ahora el dueño de For3s OS. "
                "Tú ya no tienes permisos de admin."
            )
        else:
            await q.edit_message_text(f"⚠️ No pude transferir ({motivo}). Sigues siendo el dueño.")

    async def on_recuperar_dueno(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/recuperar_dueno — PR6.2b: re-sincroniza el dueño desde la BD (fuente de
        verdad) si algo se desincronizó (owner ↔ encargado ↔ JSON). Solo el dueño."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        ok, info = await self._owners.recuperar(self._pool)
        if ok:
            await msg.reply_text(f"✅ Dueño re-sincronizado desde la BD: {info}. Todo alineado.")
        else:
            await msg.reply_text(f"⚠️ No pude recuperar ({info}).")

    async def on_reconectar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/reconectar — PR10.3a: auto-recuperación de integraciones. Reconecta el
        GitHub MCP (sesión persistente) Y VERIFICA los hermanos de red (github-mcp
        read+write, render) por HTTP. Reporta cuáles están vivos. Para el dueño.
        Es lo que se usa cuando una integración falla, SIN reinicio total."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        await msg.reply_text("🔌 Reconectando integraciones…")

        lineas: list[str] = []

        # 1) GitHub MCP (sesión persistente): recrear el cliente
        vieja = self._mcp
        self._mcp = None
        if vieja is not None:
            await vieja.aclose()
        try:
            settings = load_settings()
            pat = await SecretStore(self._pool).get_secret(settings.owner_session, "github_token")
            mcp = GitHubMCPClient(pat, read_only=True)
            await mcp.start()
            self._mcp = mcp
            lineas.append("✅ GitHub MCP (lectura): reconectado")
        except Exception:  # noqa: BLE001
            logger.exception("reconectar: fallo el GitHub MCP")
            self._mcp = None
            lineas.append("🔴 GitHub MCP (lectura): NO reconectó")

        # 2) verificar los hermanos de red por HTTP (no tienen sesión persistente)
        import os as _os

        import httpx

        hermanos = [
            ("GitHub MCP (escritura)", _os.environ.get("FOR3S_GITHUB_MCP_WRITE_URL", "http://github-mcp-write:8082/mcp")),
            ("Render (web/JS)", _os.environ.get("FOR3S_RENDER_URL", "http://render:8080/").rstrip("/") + "/health"),
        ]
        for nombre, url in hermanos:
            try:
                async with httpx.AsyncClient(timeout=6) as cli:
                    r = await cli.get(url)
                vivo = r.status_code in (200, 401, 406, 400)
                lineas.append(f"{'✅' if vivo else '⚠️'} {nombre}: {'vivo' if vivo else f'HTTP {r.status_code}'}")
            except Exception as e:  # noqa: BLE001
                lineas.append(f"🔴 {nombre}: no responde ({type(e).__name__})")

        # resumen + guía si algo sigue mal
        n_fail = sum(1 for ln in lineas if ln.startswith("🔴"))
        cabeza = "🔌 *Reconexión de integraciones*\n"
        if n_fail:
            cabeza += (
                "Algunos servicios siguen caídos. Si son los hermanos de red "
                "(render/write), están en contenedores aparte → puede que necesiten "
                "que el dueño los revise en el servidor.\n\n"
            )
        else:
            cabeza += "Todo reconectado.\n\n"
        await msg.reply_text(cabeza + "\n".join(lineas), parse_mode=ParseMode.MARKDOWN)

    async def on_reiniciar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/reiniciar — reinicio SUAVE: reconecta el GitHub MCP (sin matar proceso)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        await msg.reply_text("🔄 Reinicio suave: reconectando el GitHub MCP…")
        # Soltar la sesión vieja PRIMERO (las consultas GitHub no la usan
        # mientras reconecta). aclose() es defensivo: no explota aunque se llame
        # desde esta tarea distinta a la de setup (bug que rompía el MCP).
        vieja = self._mcp
        self._mcp = None
        if vieja is not None:
            await vieja.aclose()
        try:
            settings = load_settings()
            pat = await SecretStore(self._pool).get_secret(settings.owner_session, "github_token")
            mcp = GitHubMCPClient(pat, read_only=True)
            await mcp.start()
            self._mcp = mcp
            await msg.reply_text("✅ Listo — GitHub MCP reconectado. Todo fresco.")
        except Exception:
            logger.exception("fallo el reinicio suave del MCP")
            self._mcp = None
            await msg.reply_text(
                "⚠️ No pude reconectar el GitHub MCP. El chat sigue funcionando; "
                "si necesitas GitHub, usa /reiniciar_duro."
            )

    async def on_reiniciar_duro(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/reiniciar_duro — reinicia el PROCESO entero (systemd lo revive)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        await msg.reply_text(
            "🔄 Reinicio completo — me apago y systemd me revive en ~10s. "
            "Mándame un 'hola' en un momento para confirmar que volví."
        )
        # El bot no puede `sudo systemctl restart` (sin password). En su lugar
        # SALE con código de error → systemd (Restart=on-failure) lo relanza.
        # os._exit fuerza la salida inmediata tras avisar al usuario.
        logger.warning("reinicio duro solicitado por el dueño — saliendo para que systemd relance")
        os._exit(1)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if user is None or msg is None or not msg.text:
            return
        # FAIL-CLOSED: sin autorización no se procesa NADA.
        # H8 S10e: autorización ADITIVA (dueño / miembro / puerta abierta).
        ok, motivo = await self._autorizar(user)
        if not ok:
            if motivo == "puerta_cerrada":
                await msg.reply_text(
                    "🔴 La puerta de este equipo está cerrada. Pídele al encargado "
                    "que la abra (con /invitar) para poder entrar."
                )
            else:
                await msg.reply_text("⛔ Este bot es privado.")
            return
        assert self._pool is not None and self._agent is not None
        # Menú '/' según el rol (cosmético, idempotente — Telegram lo cachea).
        await self._publicar_menu(context.bot, msg.chat_id, user)

        # C (pulido H8): si la persona ACABA DE ENTRAR por la puerta abierta
        # (motivo 'puerta_abierta' = recién registrada), darle la bienvenida (C-ii)
        # y avisar al encargado quién entró (C-i). Solo la PRIMERA vez (después su
        # motivo es 'miembro'). Defensivo: nunca rompe el flujo del mensaje.
        if motivo == "puerta_abierta":
            await self._bienvenida_y_aviso(context, msg, user)

        # Limpiar tracking params de URLs (ej. ?fbclid=... de Facebook al pegar)
        # → el URL queda limpio (github.com/owner/repo) para detección y agente.
        texto = limpiar_urls(msg.text)

        # H8 S11: ¿amerita el EQUIPO multi-agente? Detector CONSERVADOR. Si sí,
        # lo lanzamos (gobernado por las 7 capas de cost_control) y respondemos.
        # DEFENSIVO: si el equipo falla, caemos al flujo normal de 1 agente.
        if self._amerita_equipo(texto):
            # scope de memoria: si NO es el dueño, es un miembro → memoria privada
            scope = None if self._owners.is_authorized(user.id) else user.id
            try:
                # #6: guardar el informe en el HILO de esta persona, con su autoría
                await self._correr_equipo_y_responder(
                    msg,
                    texto,
                    scope_user_id=scope,
                    sesion=await self._sesion_de(user),
                    autor_id=user.id,
                )
                return
            except Exception:  # noqa: BLE001 — el equipo no debe tumbar el bot
                logger.warning("equipo multi-agente falló, caigo a 1 agente", exc_info=True)
        # B (pulido H8): si NO es gatillo directo pero la tarea SE BENEFICIARÍA del
        # equipo → OFRECER un botón (no lanzar solo). Oferta no-bloqueante: si el
        # usuario la ignora y sigue escribiendo, el bot responde normal.
        elif self._sugiere_equipo(texto):
            self._sug_seq += 1
            sid_sug = f"s{self._sug_seq}"
            self._equipo_sugerido[sid_sug] = {"texto": texto, "user_id": user.id}
            teclado = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🤝 Lanzar equipo", callback_data=f"eqsi:{sid_sug}"),
                        InlineKeyboardButton(
                            "💬 Responde tú solo", callback_data=f"eqno:{sid_sug}"
                        ),
                    ]
                ]
            )
            try:
                await msg.reply_text(
                    "💡 Esto se beneficiaría de mi <b>equipo</b> (varios especialistas "
                    "en paralelo, tarda un poco más). ¿Lo lanzo o te respondo yo solo?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=teclado,
                )
                return
            except Exception:  # noqa: BLE001 — si falla la oferta, sigue normal
                self._equipo_sugerido.pop(sid_sug, None)
        # Hora LOCAL del usuario (2026-06-18): el servidor corre en UTC,
        # pero el usuario puede estar en otra zona. Deducimos de su language_code
        # (default CDMX) y lo inyectamos para que For3s no use la hora del servidor.
        ctx_tiempo = tiempo.contexto_temporal(getattr(user, "language_code", None))

        # #6 HILO POR USUARIO + AI2 TEMAS: sesión de la persona en su tema activo.
        # El autor se graba en cada turno (#3).
        sesion = await self._sesion_de(user)
        convo = Conversation(
            self._pool,
            self._agent,
            sesion,
            channel="telegram",
            telegram_user_id=user.id,
            scope_user_id=self._scope_de(user),
        )
        # "escribiendo..." PERSISTENTE: tarea en segundo plano que lo mantiene
        # vivo mientras el agente trabaja (análisis MCP tardan 30-60s). Se
        # cancela en el finally pase lo que pase, para no dejarla colgada.
        typing_task = asyncio.create_task(_mantener_typing(context.bot, msg.chat_id))

        # Migración MCP (Paso 4-6): si el mensaje HUELE a GitHub y el MCP está
        # disponible, el MODELO decide qué tools de GitHub usar (loop tool-use),
        # en vez del regex artesanal. Si no huele a GitHub (charla normal) o el
        # MCP está caído, va el flujo de chat normal (send). El segundo cerebro
        # responde igual; las tools solo se ofrecen cuando tienen sentido (ahorra
        # rate-limit del tool-use — ver hallazgo Paso 3).
        usa_tools = self._mcp is not None and huele_a_github(texto)

        # Anexo R3 (control por USO): si la URL es de un REPO COMPLETO (github.com/
        # owner/repo SIN /pull ni /issues), NO cabe en un solo loop → se analiza
        # archivo-por-archivo en fila de 1 (subbloques). Devuelve (owner, repo) o None.
        repo_completo = extraer_owner_repo(texto) if usa_tools else None
        # INTENCIÓN DE ESCRITURA (2026-06-18, fix routing write): si el
        # mensaje pide comentar/crear/abrir issue o PR Y trae una URL de repo, NO
        # debe ir a analizar_repo_completo (análisis) — debe ir al flujo de tools
        # (send_with_tools), que es donde el agente PROPONE la write con botón de
        # confirmación. Antes "comenta en github.com/o/r" se enrutaba a análisis,
        # intentaba listar el repo y nunca llegaba a la write. Detectamos la
        # intención y forzamos repo_completo=None para que caiga en `usa_tools`.
        _t_w = normalizar(texto)
        _PALABRAS_ESCRITURA = (
            "comenta",
            "comentar",
            "comentario",
            "crea un issue",
            "crear issue",
            "crea issue",
            "abre un issue",
            "abrir issue",
            "abre issue",
            "crea un pr",
            "crear pr",
            "crea pr",
            "abre un pr",
            "abrir pr",
            "abre pr",
            "crea un pull",
            "crear pull",
            "haz un pull",
            "responde en el issue",
            "responde al issue",
            "review",
            "revisa el pr",
        )
        quiere_escribir = usa_tools and any(p in _t_w for p in _PALABRAS_ESCRITURA)
        if quiere_escribir:
            # forzar el flujo de tools (write con confirmación), no el de análisis
            repo_completo = None
        # Modo de análisis (idea 2026-06-17): por defecto SIMPLE (orden por
        # capas de ejecución, afuera→adentro). Si pide "a profundidad/detalle/
        # minucioso/exhaustivo/a fondo" → PROFUNDO (capas + recencia). Normalizado
        # (sin acentos/mayúsculas) para que no falle por cómo lo escriba.
        _t = normalizar(texto)
        _PALABRAS_PROFUNDO = (
            "profundidad",
            "profundo",
            "minucios",
            "a detalle",
            "detallado",
            "exhaustiv",
            "a fondo",
            "completo",
            "minuciso",
        )
        modo_profundo = any(p in _t for p in _PALABRAS_PROFUNDO)
        # Intención de CONTINUAR un mapeo cortado (2026-06-17): solo si pide
        # explícitamente lo faltante/cobertura Y NO trae una URL nueva de repo.
        _PALABRAS_CONTINUAR = (
            "faltante",
            "lo que falta",
            "elementos faltant",
            "continua el analisis",
            "continua con",
            "termina el analisis",
            "lo que quedo",
            "leidos",
        )
        quiere_continuar = repo_completo is None and any(p in _t for p in _PALABRAS_CONTINUAR)
        # Detectar ORGANIZACION (github.com/NOMBRE sin /repo). Si es org, NO se
        # corre el flujo tool-use (alucinaba un repo y colgaba) -> se listan los
        # repos y se pregunta cual. Solo si NO es un repo concreto.
        org = extraer_org(texto) if (usa_tools and repo_completo is None) else None

        # Anotación #1 Nota: si hay una URL que NO es de GitHub (Luma, blog, docs),
        # For3s la LEE (fetch) en vez de decir "ábrela tú". Deja de ser cuadrado.
        url_web = None
        if not usa_tools:
            _m = re.search(r"https?://[^\s]+", texto)
            if _m and "github.com" not in _m.group(0).lower():
                url_web = _m.group(0).rstrip(").,]}")

        # Parte B (cola serial anti-rate-limit): las tareas GitHub se procesan
        # DE A UNA (un Lock) para no solapar ráfagas de tool-use que saturan el
        # rate-limit. Si ya hay una corriendo, esta espera turno y se avisa.
        # La charla normal NO usa la cola → sigue instantánea en paralelo.
        if usa_tools or quiere_continuar:
            if self._en_cola >= MAX_EN_COLA:
                typing_task.cancel()
                await _responder_seguro(
                    msg,
                    f"📋 Estoy saturado ({MAX_EN_COLA} análisis en espera). Dame "
                    "un momento a que baje la cola y reintenta — así no topo el "
                    "límite de Claude.",
                )
                return
            if self._gh_lock.locked():
                await _responder_seguro(
                    msg,
                    f"📋 En cola (hay {self._en_cola + 1} análisis antes). Lo "
                    "proceso en cuanto termine el actual — no tienes que repetir.",
                )
            elif org is not None:
                await msg.reply_text("🔍 Es una organización — déjame ver qué repos tiene…")
            else:
                await msg.reply_text(
                    "🔍 Trabajando en eso — puede tardar un momento, ya te traigo el resultado…"
                )

        try:
            # adquirir el carril serial SOLO para tareas GitHub (espera su turno)
            tengo_lock = False
            if usa_tools or quiere_continuar:
                self._en_cola += 1
                await self._gh_lock.acquire()
                self._en_cola -= 1
                tengo_lock = True
            try:
                # CONTINUAR un mapeo cortado (2026-06-17): si pidió lo
                # faltante y hay progreso pendiente guardado, retomar el mapeo REAL
                # de los archivos que faltaron, CON marcador (no improvisar).
                pendiente = None
                if quiere_continuar:
                    pendiente = await memory.get_progreso_pendiente(self._pool, sesion)
                if pendiente is not None:
                    owner_p, repo_p = pendiente["owner"], pendiente["repo"]

                    def _titulo_cont(hechos, total, prof):
                        # MISMO formato por categorías que el inicio (2026-06-18),
                        # solo cambia el encabezado → se ve como CONTINUACIÓN, no spam.
                        if prof:
                            return f"🔄 Continuando {owner_p}/{repo_p} — {hechos}/{total}\n\n"
                        return f"🔄 Continuando {owner_p}/{repo_p}…\n\n"

                    _prog_cont = crear_progreso_categorias(context, msg.chat_id, _titulo_cont)

                    resp = await asyncio.wait_for(
                        convo.continuar_repo_pendiente(
                            texto, self._mcp, pendiente, progreso=_prog_cont
                        ),
                        timeout=REPO_TIMEOUT,
                    )
                elif org is not None:
                    # Es una ORGANIZACION (github.com/NOMBRE): listar sus repos y
                    # preguntar cual analizar (evita que Claude alucine un repo y
                    # cuelgue). Es 1 llamada MCP, rapida -> timeout corto.
                    resp = await asyncio.wait_for(
                        convo.listar_repos_org(texto, self._mcp, org),
                        timeout=ANALYSIS_TIMEOUT,
                    )
                elif repo_completo is not None:
                    # Anexo R3: repo (router decide pequeño/grande adentro). Si es
                    # GRANDE, mapea por CATEGORÍAS en UN SOLO mensaje editable
                    # (pedido de diseño 2026-06-15): NO enlista archivos, muestra
                    # categorías con 3 estados (🟢 listo / 🟡 analizando / 🔴 error)
                    # + contador global i/N. Solo los ERRORES muestran el archivo.
                    owner, repo = repo_completo

                    owner, repo = repo_completo

                    def _titulo_map(hechos, total, prof):
                        if prof:
                            return f"🗂️ Mapeando {owner}/{repo} — {hechos}/{total}\n\n"
                        return f"🔍 Entendiendo {owner}/{repo}…\n\n"

                    _progreso = crear_progreso_categorias(context, msg.chat_id, _titulo_map)
                    resp = await asyncio.wait_for(
                        convo.analizar_repo_completo(
                            texto,
                            self._mcp,
                            owner,
                            repo,
                            progreso=_progreso,
                            profundo=modo_profundo,
                        ),
                        timeout=REPO_TIMEOUT,
                    )
                elif usa_tools:
                    resp = await asyncio.wait_for(
                        convo.send_with_tools(texto, self._mcp, max_tokens=2048),
                        timeout=ANALYSIS_TIMEOUT,
                    )
                elif url_web is not None:
                    # leer la URL pública y dársela a Claude para que responda
                    from for3s_core.web_fetch import fetch_url

                    ok, contenido = await fetch_url(url_web)
                    if ok:
                        prompt_web = (
                            f"El usuario compartió esta URL (no es GitHub): {url_web}\n"
                            f"Leí su contenido. Responde a lo que pide el usuario «{texto}» "
                            f"usando esta información real de la página:\n\n{contenido}"
                        )
                    else:
                        prompt_web = (
                            f"El usuario compartió {url_web} pero no pude leerla ({contenido}). "
                            f"Dile con honestidad que no pudiste abrirla y, si puedes deducir "
                            f"algo útil de la URL misma, compártelo. Mensaje del usuario: «{texto}»"
                        )
                    resp = await asyncio.wait_for(
                        convo.send(texto, max_tokens=2048, prompt=prompt_web, contexto=ctx_tiempo),
                        timeout=ANALYSIS_TIMEOUT,
                    )
                    # Apartado WEB (2026-06-19): registrar url + título +
                    # descripción (el resumen de Claude) + cuándo. SIN el HTML.
                    # El título lo saca de la cabecera "TÍTULO:" que arma web_fetch.
                    _titulo = ""
                    if ok:
                        _m = re.search(r"TÍTULO:\s*(.+)", contenido)
                        if _m:
                            _titulo = _m.group(1).strip()[:200]
                    await memory.save_consulted_web(
                        self._pool,
                        session_id=sesion,
                        url=url_web,
                        titulo=_titulo,
                        descripcion=resp.text,
                        workspace_id=sesion,
                    )
                else:
                    resp = await asyncio.wait_for(
                        convo.send(texto, max_tokens=2048, contexto=ctx_tiempo),
                        timeout=ANALYSIS_TIMEOUT,
                    )
            except TimeoutError:
                await _responder_seguro(
                    msg,
                    "⏱️ Algo se quedó atascado en esta tarea (pasó el límite de "
                    "seguridad de 8 min). No fue por tu pregunta — reintenta, y si "
                    "vuelve a pasar avísame para revisar.",
                )
                return
            except RateLimitExceeded as exc:
                await _responder_seguro(
                    msg,
                    f"⏳ Topé el límite de uso de Claude por ahora (pasa al hacer "
                    f"varias consultas seguidas a GitHub). {exc} Espera ~1 min y "
                    f"reintenta — tu pregunta estaba bien.",
                )
                return
            except ServidorSobrecargado as exc:
                await _responder_seguro(
                    msg,
                    f"🌩️ {exc} (Ya reintenté solo unas veces sin suerte.) "
                    "Tu mensaje estaba bien — vuelve a enviarlo en un momentito.",
                )
                return
            except Exception:
                logger.exception("error procesando mensaje")
                await _responder_seguro(
                    msg, "❌ Algo falló procesando tu mensaje. Intenta de nuevo."
                )
                return
        finally:
            # detener el "escribiendo..." pase lo que pase (éxito, error, timeout)
            typing_task.cancel()
            # Parte B: liberar el carril serial SOLO si ESTA invocación lo
            # adquirió (tengo_lock) → no liberar uno ajeno.
            if tengo_lock:
                self._gh_lock.release()

        # Anotación #2 Nota: enviar como HTML (código en bloque real), con
        # fallback a texto plano si el HTML falla. NUNCA deja de entregar.
        await _enviar_html(msg, resp.text)

        # WRITE TOOLS (2026-06-18): si el agente PROPUSO una escritura
        # (comentar/crear), mostrar el botón de confirmación. NADA se ha escrito
        # todavía — solo al pulsar ✅ se ejecuta (ver on_confirmar_write).
        accion = getattr(convo, "accion_pendiente", None)
        if accion:
            await self._proponer_write(msg, user, accion, context)

        # cupo: mensaje FIJADO arriba que se actualiza (decisión de diseño)
        await self._update_cupo_pin(context, msg.chat_id, resp.usage_5h, resp.usage_7d)

    # ─────────────────────── WRITE TOOLS (confirmación) ───────────────────────
    def _preview_write(self, name: str, args: dict) -> str:
        """Texto legible de QUÉ va a hacer la escritura (para el preview del botón)."""
        owner = args.get("owner", "?")
        repo = args.get("repo", "?")
        r = f"{owner}/{repo}"
        if name == "add_issue_comment":
            n = args.get("issue_number", "?")
            cuerpo = (args.get("body") or "").strip()
            return f"💬 Comentar en <b>{r}#{n}</b>:\n\n«{cuerpo[:500]}»"
        if name == "create_issue":
            t = args.get("title", "?")
            cuerpo = (args.get("body") or "").strip()
            extra = f"\n\n{cuerpo[:400]}" if cuerpo else ""
            return f"🆕 Crear issue en <b>{r}</b>:\n\n<b>{t}</b>{extra}"
        if name == "create_pull_request":
            t = args.get("title", "?")
            head = args.get("head", "?")
            base = args.get("base", "?")
            return f"🔀 Crear PR en <b>{r}</b>:\n\n<b>{t}</b>\n({head} → {base})"
        if name == "create_pull_request_review":
            n = args.get("pull_number", "?")
            ev = args.get("event", "COMMENT")
            cuerpo = (args.get("body") or "").strip()
            return f"📝 Review en PR <b>{r}#{n}</b> (event={ev}):\n\n«{cuerpo[:500]}»"
        return f"✍️ {name} en {r}"

    async def _proponer_write(self, msg, user, accion: dict, context=None) -> None:
        """Maneja una escritura GitHub propuesta. E (gate H8): bifurca por ROL —
        el DUEÑO/encargado la confirma él mismo (botón ✅/❌, como siempre); un
        MIEMBRO la PROPONE → crea una solicitud + avisa al encargado para que la
        apruebe (no se ejecuta hasta que el encargado dé el OK). NADA se escribe
        sin aprobación."""
        name = accion.get("name")
        args = accion.get("args", {})
        user_id = getattr(user, "id", None)
        # Defensa extra (además del gate del loop): solo write permitidas.
        if name not in WRITE_TOOLS_PERMITIDAS:
            await _responder_seguro(
                msg, "⛔ Esa acción no está permitida (solo comentar/crear, con confirmación)."
            )
            return
        preview = self._preview_write(name, args)

        # E — gate: si NO es admin (dueño/encargado), es un MIEMBRO → va al gate.
        if not self._es_admin(user_id) and self._equipo is not None:
            await self._proponer_write_miembro(msg, user, name, args, preview, context)
            return

        # DUEÑO/encargado: confirma él mismo (flujo de siempre).
        self._write_seq += 1
        wid = f"w{self._write_seq}"
        self._writes_pendientes[wid] = {
            "name": name,
            "args": args,
            "user_id": user_id,
            "ts": time.time(),
        }
        teclado = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirmar", callback_data=f"wok:{wid}"),
                    InlineKeyboardButton("❌ Cancelar", callback_data=f"wno:{wid}"),
                ]
            ]
        )
        try:
            await msg.reply_text(
                f"⚠️ <b>Acción de escritura en GitHub</b> — necesito tu confirmación:\n\n"
                f"{preview}\n\n<i>Nada se escribe hasta que pulses Confirmar.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=teclado,
            )
        except Exception:
            logger.exception("no pude mostrar el botón de confirmación de write")
            self._writes_pendientes.pop(wid, None)

    async def _proponer_write_miembro(self, msg, user, name, args, preview, context) -> None:
        """E — un MIEMBRO propone una write: crea solicitud (con payload tool+args)
        y avisa al ENCARGADO con [✅ Aprobar][❌ Rechazar]. La write NO se ejecuta
        hasta que el encargado apruebe (on_gate_select). DEFENSIVO."""
        import json as _json  # noqa: F401 (equipo.crear_solicitud serializa)

        owner_id = self._owners.get_owner()
        eid = await self._equipo.equipo_de(user.id)
        if eid is None or owner_id is None:
            await _responder_seguro(msg, "⛔ No puedo procesar esa acción ahora.")
            return
        nombre = getattr(user, "full_name", None) or "Un miembro"
        descripcion = f"{nombre}: {preview}"
        sid = await self._equipo.crear_solicitud(
            eid,
            user.id,
            "accion_sensible",
            descripcion,
            payload={"tool": name, "args": args, "solicitante": user.id, "nombre": nombre},
        )
        # avisar al miembro
        await msg.reply_text(
            "⏳ Esta acción de escritura en GitHub necesita la aprobación del "
            "encargado. Se la envié — te aviso cuando decida."
        )
        # avisar al ENCARGADO (proactivo) con los botones del gate
        teclado = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Aprobar", callback_data=f"gateok:{sid}"),
                    InlineKeyboardButton("❌ Rechazar", callback_data=f"gateno:{sid}"),
                ]
            ]
        )
        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text=(
                    f"🔐 <b>Solicitud de escritura</b> de un miembro:\n\n{preview}\n\n"
                    f"<i>Pedido por {nombre}. Nada se ejecuta hasta que apruebes.</i>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=teclado,
            )
        except Exception:  # noqa: BLE001
            logger.warning("no pude avisar al encargado de la solicitud de write")

    async def on_confirmar_write(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """CallbackQueryHandler de los botones ✅/❌ de una escritura propuesta."""
        q = update.callback_query
        if q is None or not q.data:
            return
        await q.answer()  # quita el "reloj" del botón
        accion_cb, _, wid = q.data.partition(":")
        pend = self._writes_pendientes.pop(wid, None)

        # Solo el dueño de la acción puede confirmarla (y debe estar autorizado).
        user_id = q.from_user.id if q.from_user else None
        if pend is None:
            await q.edit_message_text(
                "⏳ Esa acción ya expiró o se resolvió. Pídela de nuevo si la necesitas."
            )
            return
        if user_id != pend["user_id"] or not self._owners.is_authorized(user_id):
            await q.edit_message_text("⛔ No puedes confirmar esta acción.")
            return
        # Expiración (5 min, como el approval timeout del diseño R4.2.1).
        if time.time() - pend["ts"] > 300:
            await q.edit_message_text("⏱️ La confirmación expiró (5 min). Pide la acción de nuevo.")
            return

        if accion_cb == "wno":
            await audit.append(
                self._pool,
                actor="user",
                action="github_write_cancelado",
                detail={"tool": pend["name"], "args": pend["args"]},
            )
            await q.edit_message_text("❌ Cancelado. No se escribió nada en GitHub.")
            return

        # accion_cb == "wok" → EJECUTAR la escritura (contenedor MCP efímero write).
        name, args = pend["name"], pend["args"]
        if name not in WRITE_TOOLS_PERMITIDAS or self._pat is None:
            await q.edit_message_text("⛔ Acción no permitida o GitHub no disponible.")
            return
        await q.edit_message_text("⏳ Ejecutando en GitHub…")
        try:
            resultado = await ejecutar_write(self._pat, name, args)
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("falló la ejecución de la write tool %s", name)
            resultado = f"{type(exc).__name__}: {exc}"
            ok = False
        # AUDIT inmutable de la escritura (pase lo que pase).
        await audit.append(
            self._pool,
            actor="user",
            action="github_write",
            detail={"tool": name, "args": args, "ok": ok, "result": resultado[:1000]},
        )
        if ok:
            await q.edit_message_text(
                f"✅ Hecho en GitHub.\n\n<code>{(resultado or '')[:600]}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await q.edit_message_text(f"❌ No se pudo ejecutar: {resultado[:400]}")

    async def on_adjunto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Procesa una FOTO o un DOCUMENTO (PDF/Word/Excel) adjunto (multimodal,
        2026-06-18). Lo descarga de Telegram, lo convierte en bloques de
        contenido (multimodal.procesar_adjunto) y lo manda a Claude para que lo
        "vea"/"lea". El caption del usuario es la pregunta ("¿qué dice este PDF?").

        En MEMORIA se guarda solo una NOTA de texto (no el base64 enorme): qué
        tipo de archivo mandó + su caption, para que el historial tenga sentido.
        """
        user = update.effective_user
        msg = update.message
        if user is None or msg is None:
            return
        if not self._owners.is_authorized(user.id):  # FAIL-CLOSED
            await msg.reply_text("⛔ Este bot es privado.")
            return
        assert self._pool is not None and self._agent is not None

        # ¿foto o documento? Telegram manda PHOTO en varias resoluciones
        # (tomamos la más grande) o un Document (PDF/Word/Excel/imagen-como-archivo).
        tg_file = None
        nombre = ""
        mime = ""
        if msg.photo:
            tg_file = msg.photo[-1]  # la última = mayor resolución
            nombre = "foto.jpg"
            mime = "image/jpeg"
        elif msg.document:
            tg_file = msg.document
            nombre = msg.document.file_name or "archivo"
            mime = msg.document.mime_type or ""
        if tg_file is None:
            return

        ctx_tiempo = tiempo.contexto_temporal(getattr(user, "language_code", None))
        # #6 hilo por usuario + AI2 tema activo
        sesion = await self._sesion_de(user)
        convo = Conversation(
            self._pool,
            self._agent,
            sesion,
            channel="telegram",
            telegram_user_id=user.id,
            scope_user_id=self._scope_de(user),
        )
        typing_task = asyncio.create_task(_mantener_typing(context.bot, msg.chat_id))
        etiqueta = multimodal.descripcion_corta(nombre, mime)
        caption = (msg.caption or "").strip()

        try:
            # descargar los bytes del archivo desde Telegram
            try:
                archivo = await context.bot.get_file(tg_file.file_id)
                datos = bytes(await archivo.download_as_bytearray())
            except Exception:
                logger.exception("no pude descargar el adjunto")
                typing_task.cancel()
                await _responder_seguro(
                    msg, "❌ No pude descargar ese archivo de Telegram. Reintenta, por favor."
                )
                return

            # convertir a bloques multimodales (imagen/PDF → base64; Word/Excel → texto)
            try:
                bloques = multimodal.procesar_adjunto(datos, nombre=nombre, mime=mime)
            except multimodal.ArchivoNoSoportado as exc:
                typing_task.cancel()
                await _responder_seguro(msg, f"📎 {exc}")
                return

            # la pregunta: el caption si lo hay, o una instrucción por defecto
            if caption:
                pregunta = caption
            else:
                pregunta = (
                    f"Te mando {('una ' if etiqueta == 'imagen' else 'un ')}{etiqueta}. "
                    "Descríbeme/resúmeme su contenido y dime lo más relevante."
                )
            # lo que se GUARDA en memoria (corto, sin base64); lo que se MANDA a
            # Claude lleva los bloques como adjuntos.
            nota_memoria = f"[{etiqueta}: {nombre}] {pregunta}"

            try:
                resp = await asyncio.wait_for(
                    convo.send(
                        nota_memoria,
                        prompt=pregunta,
                        max_tokens=2048,
                        contexto=ctx_tiempo,
                        adjuntos=bloques,
                    ),
                    timeout=ANALYSIS_TIMEOUT,
                )
            except TimeoutError:
                await _responder_seguro(
                    msg,
                    "⏱️ Ese archivo tardó demasiado en procesarse. Reintenta, "
                    "y si vuelve a pasar avísame.",
                )
                return
            except RateLimitExceeded as exc:
                await _responder_seguro(
                    msg,
                    f"⏳ Topé el límite de uso de Claude por ahora. {exc} "
                    "Espera ~1 min y reintenta.",
                )
                return
            except Exception:
                logger.exception("error procesando adjunto")
                await _responder_seguro(msg, "❌ Algo falló leyendo ese archivo. Intenta de nuevo.")
                return
        finally:
            typing_task.cancel()

        await _enviar_html(msg, resp.text)
        # Apartado ARCHIVOS (2026-06-19): registrar tipo + nombre + resumen
        # (el análisis de Claude) + cuándo. SIN el binario. Defensivo.
        await memory.save_consulted_file(
            self._pool,
            session_id=sesion,
            tipo=etiqueta,
            nombre=nombre,
            resumen=resp.text,
            workspace_id=sesion,
        )
        await self._update_cupo_pin(context, msg.chat_id, resp.usage_5h, resp.usage_7d)


def _resolver_token_telegram(settings) -> str:
    """Resuelve el token de Telegram, priorizando el SecretStore CIFRADO.

    Orden:
      1. SecretStore (Postgres, AES-256-GCM + KEK) — la fuente segura.
      2. Fallback al .env (TELEGRAM_BOT_TOKEN) — primer arranque / migración.

    Así el token vive cifrado en reposo; el .env solo guarda el mínimo (y se
    puede vaciar tras migrar). Si la BD no responde, el .env evita un caído.
    """

    async def _leer_de_bd() -> str | None:
        try:
            pool = await db.connect(settings.database_url)
        except Exception as exc:  # BD no disponible → caemos al .env
            logger.warning("No pude conectar a la BD para el token de Telegram: %s", exc)
            return None
        try:
            return await SecretStore(pool).get_secret(settings.owner_session, "telegram_bot_token")
        finally:
            await pool.close()

    cifrado = asyncio.run(_leer_de_bd())
    if cifrado:
        logger.info("token de Telegram cargado desde SecretStore cifrado")
        return cifrado
    if settings.telegram_bot_token:
        logger.info("token de Telegram cargado desde .env (fallback)")
        return settings.telegram_bot_token
    return ""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # SEGURIDAD: httpx loguea cada URL en INFO, y el token de Telegram va EN la
    # URL (api.telegram.org/bot<TOKEN>/...). En INFO eso filtra el token a los
    # logs en texto plano (miles de líneas). Lo subimos a WARNING: deja de
    # loguear cada request (0 fuga de token) pero seguimos viendo errores reales.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()

    token = _resolver_token_telegram(settings)
    if not token:
        print("Falta el token de Telegram (ni en SecretStore cifrado ni en .env)")
        return 1

    store = OwnerStore(Path.cwd() / ".for3s" / "telegram_owner.json")
    pin_store = CupoPinStore(Path.cwd() / ".for3s" / "telegram_cupo_pin.json")
    channel = TelegramChannel(store, settings.owner_session, pin_store)

    app = (
        Application.builder()
        .token(token)
        # Timeouts tolerantes: la red del servidor a Telegram es lenta (~300ms RTT,
        # relay Tailscale) y parpadea. Defaults cortos cortaban requests legítimos.
        .connect_timeout(20.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(20.0)
        .post_init(channel.setup)
        .post_shutdown(channel.teardown)
        .build()
    )
    app.add_handler(CommandHandler("start", channel.on_start))
    app.add_handler(CommandHandler("cupo", channel.on_cupo))
    # comandos de administración (solo dueño/admin)
    app.add_handler(CommandHandler("ayuda", channel.on_ayuda))  # PR10.1 soporte
    app.add_handler(CommandHandler("estado", channel.on_estado))
    app.add_handler(CommandHandler("version", channel.on_version))  # AI5
    app.add_handler(CommandHandler("perfil", channel.on_perfil))  # P1
    app.add_handler(CommandHandler("skills", channel.on_skills))  # H10
    app.add_handler(CommandHandler("aprende", channel.on_aprende))  # H12 P1
    app.add_handler(CommandHandler("model", channel.on_model))
    app.add_handler(CommandHandler("autogen", channel.on_autogen))  # H11: kill switch
    app.add_handler(CommandHandler("dmn", channel.on_dmn))  # H9: SUEÑA (DMN)
    app.add_handler(CommandHandler("salud", channel.on_salud))  # PR2 monitoreo
    app.add_handler(CommandHandler("datos", channel.on_datos))  # PR3 analitica
    app.add_handler(CommandHandler("diagnostico", channel.on_diagnostico))
    app.add_handler(CommandHandler("transferir_dueno", channel.on_transferir))  # PR6.2a
    app.add_handler(CommandHandler("recuperar_dueno", channel.on_recuperar_dueno))  # PR6.2b
    app.add_handler(CallbackQueryHandler(channel.on_transferir_select, pattern=r"^transf:"))  # PR6.2a
    app.add_handler(CommandHandler("reconectar", channel.on_reconectar))  # PR10.3a
    app.add_handler(CommandHandler("reiniciar", channel.on_reiniciar))
    app.add_handler(CommandHandler("reiniciar_duro", channel.on_reiniciar_duro))
    app.add_handler(CommandHandler("invitar", channel.on_invitar))  # H8 S10e: la puerta
    app.add_handler(CommandHandler("tema", channel.on_tema))  # AI2: cambiar tema
    app.add_handler(CommandHandler("temas", channel.on_temas))  # AI2: listar temas
    app.add_handler(CommandHandler("hilos", channel.on_hilos))  # AI7: vista hilos
    app.add_handler(CommandHandler("miembros", channel.on_miembros))  # AI7: equipo
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, channel.on_message))
    # Multimodal (2026-06-18): fotos y documentos (PDF/Word/Excel). Van
    # ANTES del catch-all de texto y por su propio handler on_adjunto.
    app.add_handler(MessageHandler(filters.PHOTO, channel.on_adjunto))
    app.add_handler(MessageHandler(filters.Document.ALL, channel.on_adjunto))
    # Write tools (2026-06-18): botones ✅/❌ de confirmación de escritura.
    app.add_handler(CallbackQueryHandler(channel.on_confirmar_write, pattern=r"^w(ok|no):"))
    app.add_handler(CallbackQueryHandler(channel.on_model_select, pattern=r"^model:"))
    # H8 S10e: botones de la puerta + del gate de aprobación del encargado.
    app.add_handler(CallbackQueryHandler(channel.on_puerta_select, pattern=r"^puerta:"))
    app.add_handler(CallbackQueryHandler(channel.on_gate_select, pattern=r"^gate(ok|no):"))
    app.add_handler(CallbackQueryHandler(channel.on_tema_select, pattern=r"^tema:"))  # AI2
    app.add_handler(CallbackQueryHandler(channel.on_equipo_sugerido, pattern=r"^eq(si|no):"))  # B
    app.add_handler(CallbackQueryHandler(channel.on_skill_gate, pattern=r"^sk(ok|no):"))  # H12 P2
    app.add_handler(CallbackQueryHandler(channel.on_dmn_propuesta, pattern=r"^dp(ok|no):"))  # H9-c
    app.add_handler(CallbackQueryHandler(channel.on_kick, pattern=r"^kick:"))  # C-v
    app.add_handler(CallbackQueryHandler(channel.on_kick_confirm, pattern=r"^k(ok|no):"))
    app.add_error_handler(channel.on_error)  # red inestable → no morir, no ensuciar logs

    logger.info("For3s OS Telegram: arrancando polling...")
    # drop_pending_updates también borra el webhook si lo hubiera
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
