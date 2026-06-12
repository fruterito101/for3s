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

import json
import logging
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
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

logger = logging.getLogger("for3s.telegram")

# Límite real de Telegram por mensaje (lección Hermes: partir respuestas largas).
MAX_MESSAGE_LENGTH = 4096

# A partir de este % de cupo usado (suscripción, ventana 5h) → alerta visible.
ALERT_THRESHOLD = 0.80


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

    def __init__(self, owner_store: OwnerStore, owner_session: str) -> None:
        self._owners = owner_store
        self._owner_session = owner_session
        self._pool = None
        self._agent: Agent | None = None

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

    async def on_cupo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        user = update.effective_user
        if msg is None or user is None:
            return
        if not self._owners.is_authorized(user.id):
            await msg.reply_text("⛔ Este bot es privado.")
            return
        assert self._agent is not None
        try:
            # llamada mínima solo para leer los headers de cupo actuales
            resp = self._agent.ask_with_history([{"role": "user", "content": "ok"}], max_tokens=5)
        except RateLimitExceeded as exc:
            await msg.reply_text(f"⏳ {exc}")
            return
        cupo = format_cupo(resp.usage_5h, resp.usage_7d)
        await msg.reply_text(cupo if cupo else "🔋 cupo no disponible ahora mismo.")

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
        convo = Conversation(self._pool, self._agent, self._owner_session)
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        try:
            # Nota: el provider es síncrono por dentro (bloquea el loop unos
            # segundos durante la llamada a Claude). Aceptable con 1 usuario;
            # R3 lo vuelve async (httpx.AsyncClient).
            resp = await convo.send(msg.text)
        except RateLimitExceeded as exc:
            await msg.reply_text(f"⏳ {exc}")
            return
        except Exception:
            logger.exception("error procesando mensaje")
            await msg.reply_text("❌ Algo falló procesando tu mensaje. Intenta de nuevo.")
            return

        for chunk in split_message(resp.text):
            await msg.reply_text(chunk)

        # pie de cupo / alerta al 80% (decisión de Brian)
        cupo = format_cupo(resp.usage_5h, resp.usage_7d)
        if cupo:
            await msg.reply_text(cupo)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = load_settings()
    if not settings.telegram_bot_token:
        print("Falta TELEGRAM_BOT_TOKEN en el .env")
        return 1

    store = OwnerStore(Path.cwd() / ".for3s" / "telegram_owner.json")
    channel = TelegramChannel(store, settings.owner_session)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
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
