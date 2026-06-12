"""Tests de los HANDLERS del bot Telegram (E2) — con mocks, sin red.

Cubre el comportamiento que antes solo se probaba a mano: registro de dueño,
fail-closed contra extraños, y /cupo usando el dato guardado (cero tokens).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from for3s_core.telegram_channel import CupoPinStore, OwnerStore, TelegramChannel


def _fake_update(user_id: int, text: str = "hola"):
    """Construye un Update falso de Telegram (user + message con reply_text async)."""
    upd = MagicMock()
    upd.effective_user.id = user_id
    upd.effective_user.full_name = f"user{user_id}"
    upd.message.text = text
    upd.message.chat_id = 555
    upd.message.reply_text = AsyncMock()
    return upd


def _channel(tmp_path):
    owners = OwnerStore(tmp_path / "owner.json")
    pins = CupoPinStore(tmp_path / "pin.json")
    return TelegramChannel(owners, "brian", pins), owners


async def test_primer_start_registra_dueno(tmp_path) -> None:
    ch, owners = _channel(tmp_path)
    upd = _fake_update(111)
    await ch.on_start(upd, MagicMock())
    assert owners.get_owner() == 111  # quedó registrado
    upd.message.reply_text.assert_awaited()  # respondió algo


async def test_start_de_extrano_bloqueado(tmp_path) -> None:
    ch, owners = _channel(tmp_path)
    owners.set_owner(111)  # ya hay dueño
    upd = _fake_update(999)  # otro usuario
    await ch.on_start(upd, MagicMock())
    # el extraño NO se vuelve dueño
    assert owners.get_owner() == 111
    txt = upd.message.reply_text.await_args[0][0]
    assert "privado" in txt.lower()


async def test_on_message_fail_closed_sin_dueno(tmp_path) -> None:
    ch, _ = _channel(tmp_path)  # sin dueño registrado
    upd = _fake_update(111, "analiza esto")
    await ch.on_message(upd, MagicMock())
    txt = upd.message.reply_text.await_args[0][0]
    assert "privado" in txt.lower()  # nadie pasa sin dueño


async def test_on_message_extrano_bloqueado(tmp_path) -> None:
    ch, owners = _channel(tmp_path)
    owners.set_owner(111)
    upd = _fake_update(999, "hola")  # no es el dueño
    await ch.on_message(upd, MagicMock())
    txt = upd.message.reply_text.await_args[0][0]
    assert "privado" in txt.lower()


async def test_cupo_sin_dato_avisa(tmp_path) -> None:
    ch, owners = _channel(tmp_path)
    owners.set_owner(111)
    upd = _fake_update(111)
    await ch.on_cupo(upd, MagicMock())
    txt = upd.message.reply_text.await_args[0][0]
    assert "no tengo dato" in txt.lower() or "🔋" in txt


async def test_cupo_con_dato_guardado_no_llama_claude(tmp_path) -> None:
    ch, owners = _channel(tmp_path)
    owners.set_owner(111)
    ch._last_cupo = (0.42, 0.20)  # dato guardado (vino gratis antes)
    ch._agent = MagicMock()  # si llamara a Claude, este mock lo detectaría
    upd = _fake_update(111)
    await ch.on_cupo(upd, MagicMock())
    txt = upd.message.reply_text.await_args[0][0]
    assert "42%" in txt
    ch._agent.ask_with_history.assert_not_called()  # CERO tokens


async def test_cupo_extrano_bloqueado(tmp_path) -> None:
    ch, owners = _channel(tmp_path)
    owners.set_owner(111)
    upd = _fake_update(999)
    await ch.on_cupo(upd, MagicMock())
    txt = upd.message.reply_text.await_args[0][0]
    assert "privado" in txt.lower()
