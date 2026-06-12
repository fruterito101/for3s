"""Tests de config.py (E4) — carga de .env y autodetección de modo de auth."""

from __future__ import annotations

import pytest
from for3s_core.config import load_settings


def test_detecta_oauth_por_prefijo(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FOR3S_AUTH_MODE", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "ANTHROPIC_TOKEN=sk-ant-oat01-xxx\nDATABASE_URL=postgresql://x\n", encoding="utf-8"
    )
    # limpiar el entorno para que lea del archivo
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    s = load_settings(env_path=env)
    assert s.auth_mode == "oauth"
    assert s.is_oauth is True


def test_detecta_apikey_por_prefijo(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FOR3S_AUTH_MODE", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "ANTHROPIC_TOKEN=sk-ant-api03-yyy\nDATABASE_URL=postgresql://x\n", encoding="utf-8"
    )
    s = load_settings(env_path=env)
    assert s.auth_mode == "apikey"
    assert s.is_oauth is False


def test_falta_token_lanza_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    env = tmp_path / "vacio.env"
    env.write_text("DATABASE_URL=postgresql://x\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        load_settings(env_path=env)


def test_lee_telegram_token_y_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FOR3S_OWNER_SESSION", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "ANTHROPIC_TOKEN=sk-ant-oat01-x\nDATABASE_URL=postgresql://x\n"
        "TELEGRAM_BOT_TOKEN=123:abc\nFOR3S_OWNER_SESSION=brian\n",
        encoding="utf-8",
    )
    s = load_settings(env_path=env)
    assert s.telegram_bot_token == "123:abc"
    assert s.owner_session == "brian"
