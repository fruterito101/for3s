"""Canal Telegram de For3s OS (H3) — el bot que conecta tu celular al cerebro.

Reusa el cerebro completo (Conversation = memoria H2 + Claude H1 + audit):
este módulo solo es la "puerta de entrada" de Telegram.

Patrón tomado del análisis del código fuente de Hermes (gateway/platforms/
telegram.py): POLLING (sin puertos públicos) + allowlist FAIL-CLOSED ("sin
lista = denegar por default") + split de respuestas a 4,096 chars.

Acceso (decisión de Brian 2026-06-11): el PRIMER /start registra al dueño;
después, todo lo demás queda bloqueado. El dueño comparte memoria con el CLI
(sesión "brian"). Multi-usuario formal llega en H13 (auth/RBAC).

Cupo (decisión de Brian 2026-06-11): cada respuesta muestra el cupo de la
suscripción usado; a partir del 80% alerta visible; /cupo lo consulta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from for3s_core import db
from for3s_core.agent import Agent
from for3s_core.config import load_settings
from for3s_core.conversation import Conversation, huele_a_github
from for3s_core.llm import ClaudeProvider, RateLimitExceeded
from for3s_core.mcp_client import GitHubMCPClient
from for3s_core.secret_store import SecretStore
from for3s_core.text_normalize import limpiar_urls

logger = logging.getLogger("for3s.telegram")

# Límite real de Telegram por mensaje (lección Hermes: partir respuestas largas).
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
TYPING_REFRESH = 4  # cada cuántos seg re-enviar "escribiendo..." (Telegram lo apaga a los ~5s)
MAX_EN_COLA = 3  # Parte B: máx tareas GitHub esperando turno; más allá, se rechaza
# H-A: umbral para avisar "esto puede tardar" — toda tarea que use tools de
# GitHub (huele_a_github) manda un aviso inicial, porque suelen tardar 30-60s.


async def _mantener_typing(bot, chat_id: int) -> None:
    """Mantiene VIVO el indicador 'escribiendo...' hasta que se cancele.

    Telegram apaga el typing a los ~5s por cada send_chat_action. Los análisis
    con MCP tardan 30-60s; sin esto, el indicador desaparece y parece que el
    bot se colgó (reportado por Brian). Esta tarea re-envía TYPING en bucle;
    on_message la cancela cuando llega la respuesta.
    """
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(TYPING_REFRESH)
    except asyncio.CancelledError:
        pass  # cancelada al terminar el proceso: salida limpia


async def _responder_seguro(msg, texto: str) -> None:
    """Manda un mensaje al usuario reintentando si Telegram falla.

    H-A/UX: NUNCA dejar al usuario sin desenlace. Si un aviso de error o
    resultado no se envía a la primera (red, rate-limit de Telegram), reintenta
    un par de veces. Si aun así falla, lo registra — pero al menos lo intentó.
    """
    for intento in range(3):
        try:
            await msg.reply_text(texto)
            return
        except Exception:
            if intento < 2:
                await asyncio.sleep(2)
            else:
                logger.exception("no pude entregar el mensaje al usuario tras 3 intentos")


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
    """Guarda quién es el dueño del bot (el primer /start). Fail-closed."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get_owner(self) -> int | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            owner = data.get("owner_id")
            return int(owner) if owner is not None else None
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return None

    def set_owner(self, user_id: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"owner_id": user_id}), encoding="utf-8")
        self._path.chmod(0o600)

    def is_authorized(self, user_id: int | None) -> bool:
        """FAIL-CLOSED: sin dueño registrado o sin user_id → denegado."""
        owner = self.get_owner()
        return owner is not None and user_id is not None and user_id == owner


class TelegramChannel:
    """La puerta de Telegram hacia el cerebro de For3s."""

    def __init__(
        self, owner_store: OwnerStore, owner_session: str, pin_store: CupoPinStore
    ) -> None:
        self._owners = owner_store
        self._owner_session = owner_session
        self._pins = pin_store
        self._pool = None
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

    async def setup(self, app: Application) -> None:
        """post_init de PTB: conecta el cerebro (pool + provider)."""
        settings = load_settings()
        self._pool = await db.connect(settings.database_url)
        await db.apply_migrations(self._pool)
        provider = ClaudeProvider(
            token=settings.anthropic_token, oauth=settings.is_oauth, model=settings.model
        )
        self._agent = Agent(provider)
        self._model = settings.model
        logger.info("cerebro conectado (modelo=%s auth=%s)", settings.model, settings.auth_mode)

        # MCP GitHub (Paso 4-6): sesión persistente, PAT del SecretStore (KEK).
        # Si falla (Docker abajo, etc.) el bot sigue: degrada a sin-GitHub.
        try:
            pat = await SecretStore(self._pool).get_secret(settings.owner_session, "github_token")
            mcp = GitHubMCPClient(pat, read_only=True)
            await mcp.start()
            self._mcp = mcp
            logger.info("GitHub MCP conectado (read-only)")
        except Exception:
            logger.exception("no pude iniciar el GitHub MCP (sigo sin GitHub)")
            self._mcp = None

    async def teardown(self, app: Application) -> None:
        """post_shutdown de PTB: cierra el pool de BD ordenadamente.

        Esto SÍ se await-ea bien en el shutdown (verificado: sin errores de
        pool al apagar). Nota: al recibir SIGTERM verás en los logs
        "RuntimeWarning: coroutine 'Updater.stop' was never awaited" — es un
        warning COSMÉTICO interno de python-telegram-bot 22.x: en la carrera
        de la señal, PTB evalúa self.updater.running y la corrutina stop()
        queda sin await. NO es bug nuestro; el apagado es correcto (pool y
        application se cierran). Decisión (Brian, 2026-06-13): dejarlo, no
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

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return
        owner = self._owners.get_owner()
        if owner is None:
            self._owners.set_owner(user.id)
            logger.info("dueño registrado: %s (%s)", user.id, user.full_name)
            await update.message.reply_text(
                "👑 Quedaste registrado como dueño de For3s OS.\n"
                "Escríbeme lo que quieras — recuerdo nuestras conversaciones.\n"
                "Usa /cupo para ver cuánto te queda de tu suscripción."
            )
        elif user.id == owner:
            await update.message.reply_text("🦊 Hola de nuevo. Te escucho. (/cupo para tu cupo)")
        else:
            await update.message.reply_text("⛔ Este bot es privado.")

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

    async def on_cupo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        user = update.effective_user
        if msg is None or user is None:
            return
        if not self._owners.is_authorized(user.id):
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

    async def on_estado(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/estado — salud rápida del agente (cero tokens)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
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

    async def on_diagnostico(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/diagnostico — mini-reporte: últimos turnos + tools recientes."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        if self._pool is None:
            await msg.reply_text("🩺 Aún sin conexión a la BD.")
            return
        from for3s_core import memory

        turns = await memory.load_history(self._pool, self._owner_session, last_n=4)
        lineas = ["🩺 *Diagnóstico rápido*", f"Últimos {len(turns)} turnos:"]
        for t in turns:
            quien = "👤" if t.role == "user" else "🤖"
            lineas.append(f"{quien} {t.content[:60].replace(chr(10), ' ')}")
        mcp = "✅" if self._mcp is not None else "❌"
        lineas.append(f"\nGitHub MCP: {mcp} · modelo: {self._model}")
        await msg.reply_text("\n".join(lineas))

    async def on_reiniciar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/reiniciar — reinicio SUAVE: reconecta el GitHub MCP (sin matar proceso)."""
        msg, user = update.message, update.effective_user
        if msg is None or user is None:
            return
        if not self._es_admin(user.id):
            await msg.reply_text("⛔ Comando solo para el dueño.")
            return
        await msg.reply_text("🔄 Reinicio suave: reconectando el GitHub MCP…")
        try:
            if self._mcp is not None:
                await self._mcp.aclose()
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
        # FAIL-CLOSED (lección Hermes): sin autorización no se procesa NADA.
        if not self._owners.is_authorized(user.id):
            await msg.reply_text("⛔ Este bot es privado.")
            return
        assert self._pool is not None and self._agent is not None

        # Limpiar tracking params de URLs (ej. ?fbclid=... de Facebook al pegar)
        # → el URL queda limpio (github.com/owner/repo) para detección y agente.
        texto = limpiar_urls(msg.text)

        # memoria COMPARTIDA con el CLI (decisión de Brian): sesión del dueño.
        convo = Conversation(self._pool, self._agent, self._owner_session, channel="telegram")
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

        # Parte B (cola serial anti-rate-limit): las tareas GitHub se procesan
        # DE A UNA (un Lock) para no solapar ráfagas de tool-use que saturan el
        # rate-limit. Si ya hay una corriendo, esta espera turno y se avisa.
        # La charla normal NO usa la cola → sigue instantánea en paralelo.
        if usa_tools:
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
            else:
                await msg.reply_text(
                    "🔍 Trabajando en eso — puede tardar un momento, ya te traigo el resultado…"
                )

        try:
            # adquirir el carril serial SOLO para tareas GitHub (espera su turno)
            tengo_lock = False
            if usa_tools:
                self._en_cola += 1
                await self._gh_lock.acquire()
                self._en_cola -= 1
                tengo_lock = True
            try:
                if usa_tools:
                    resp = await asyncio.wait_for(
                        convo.send_with_tools(texto, self._mcp, max_tokens=2048),
                        timeout=ANALYSIS_TIMEOUT,
                    )
                else:
                    resp = await asyncio.wait_for(
                        convo.send(texto, max_tokens=2048),
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

        for chunk in split_message(md_to_telegram(resp.text)):
            await msg.reply_text(chunk)

        # cupo: mensaje FIJADO arriba que se actualiza (decisión de Brian)
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
        .post_init(channel.setup)
        .post_shutdown(channel.teardown)
        .build()
    )
    app.add_handler(CommandHandler("start", channel.on_start))
    app.add_handler(CommandHandler("cupo", channel.on_cupo))
    # comandos de administración (solo dueño/admin)
    app.add_handler(CommandHandler("estado", channel.on_estado))
    app.add_handler(CommandHandler("diagnostico", channel.on_diagnostico))
    app.add_handler(CommandHandler("reiniciar", channel.on_reiniciar))
    app.add_handler(CommandHandler("reiniciar_duro", channel.on_reiniciar_duro))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, channel.on_message))

    logger.info("For3s OS Telegram: arrancando polling...")
    # drop_pending_updates también borra el webhook si lo hubiera (lección Hermes)
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
