"""Configuración de For3s OS — lee secrets de entorno/.env (nunca del repo).

H1.1 — soporta los DOS modos de auth con Claude:
  • oauth  → token de suscripción (sk-ant-oat01-...), sin pago por consumo.
  • apikey → API key estándar (sk-ant-api03-...), pago por token.
El modo se autodetecta por el prefijo del token, o se fuerza con FOR3S_AUTH_MODE.

H2 — añade database_url (PostgreSQL) para la memoria persistente.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Carga simple de .env (sin dependencias). Solo claves no presentes ya."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    """Configuración resuelta de For3s OS."""

    anthropic_token: str
    auth_mode: str  # "oauth" | "apikey"
    model: str
    database_url: str

    @property
    def is_oauth(self) -> bool:
        return self.auth_mode == "oauth"


def load_settings(env_path: Path | None = None) -> Settings:
    """Resuelve la configuración desde .env + variables de entorno."""
    _load_dotenv(env_path or Path.cwd() / ".env")

    token = os.environ.get("ANTHROPIC_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Falta ANTHROPIC_TOKEN en el entorno o .env. "
            "Pon tu token de suscripción (sk-ant-oat01-...) o API key (sk-ant-api03-...)."
        )

    forced = os.environ.get("FOR3S_AUTH_MODE", "").strip().lower()
    if forced in ("oauth", "apikey"):
        auth_mode = forced
    elif token.startswith("sk-ant-oat"):
        auth_mode = "oauth"
    else:
        auth_mode = "apikey"

    model = os.environ.get("FOR3S_MODEL", "claude-sonnet-4-6").strip()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    return Settings(
        anthropic_token=token, auth_mode=auth_mode, model=model, database_url=database_url
    )
