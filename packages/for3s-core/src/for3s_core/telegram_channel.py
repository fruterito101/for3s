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
from for3s_core.conversation import Conversation
from for3s_core.llm import ClaudeProvider, RateLimitExceeded
from for3s_core.pr_review import analizar_pr
from for3s_core.secret_store import SecretStore

logger = logging.getLogger("for3s.telegram")

# Límite real de Telegram por mensaje (lección Hermes: partir respuestas largas).
MAX_MESSAGE_LENGTH = 4096

# A partir de este % de cupo usado (suscripción, ventana 5h) → alerta visible.
ALERT_THRESHOLD = 0.80

# Timeouts (segundos) para no quedar congelados en operaciones largas.
GITHUB_TIMEOUT = 60  # traer el recurso de GitHub (+ lint sandbox)
ANALYSIS_TIMEOUT = 120  # análisis completo con Claude


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
        logger.info("cerebro conectado (modelo=%s auth=%s)", settings.model, settings.auth_mode)

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

        # memoria COMPARTIDA con el CLI (decisión de Brian): sesión del dueño.
        convo = Conversation(self._pool, self._agent, self._owner_session, channel="telegram")
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)

        # H4: ¿el mensaje trae un recurso de GitHub? → enriquecer con QA.
        # TODO con TIMEOUT: si tarda demasiado (PR enorme, red lenta) cortamos
        # y avisamos, en vez de quedar congelados en silencio (bug encontrado
        # por Brian con el PR #134). asyncio.wait_for garantiza recuperación.
        # prompt enriquecido (lo que se manda a Claude) ≠ msg.text (lo que se
        # guarda en memoria). Así un PR de 100k chars NO se guarda como turno.
        prompt = None
        try:
            enriched = await asyncio.wait_for(
                analizar_pr(self._pool, self._owner_session, msg.text),
                timeout=GITHUB_TIMEOUT,
            )
        except TimeoutError:
            await msg.reply_text(
                "⏱️ Tardé demasiado trayendo ese recurso de GitHub (¿es muy grande?). "
                "Intenta con algo más pequeño o un archivo específico."
            )
            return
        except Exception:
            logger.exception("error trayendo el recurso de GitHub")
            enriched = None
        if enriched is not None:
            if enriched.startswith("__DIRECT__"):  # error legible del tool
                await msg.reply_text(enriched.removeprefix("__DIRECT__"))
                return
            prompt = enriched  # solo para Claude; en memoria queda msg.text

        try:
            # Nota: el provider es síncrono por dentro. wait_for + to_thread
            # evitan que un análisis largo congele al bot (R3 lo hará async).
            resp = await asyncio.wait_for(
                convo.send(msg.text, max_tokens=2048, prompt=prompt),
                timeout=ANALYSIS_TIMEOUT,
            )
        except TimeoutError:
            await msg.reply_text(
                "⏱️ El análisis tardó demasiado (más de 2 min). Suele pasar con "
                "código muy grande. Intenta con algo más acotado."
            )
            return
        except RateLimitExceeded as exc:
            await msg.reply_text(f"⏳ {exc}")
            return
        except Exception:
            logger.exception("error procesando mensaje")
            await msg.reply_text("❌ Algo falló procesando tu mensaje. Intenta de nuevo.")
            return

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, channel.on_message))

    logger.info("For3s OS Telegram: arrancando polling...")
    # drop_pending_updates también borra el webhook si lo hubiera (lección Hermes)
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
