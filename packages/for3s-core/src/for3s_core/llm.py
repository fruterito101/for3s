"""Capa LLM de For3s OS — el motor generativo (Nodo 3 PFC, versión H1).

H1.2 LLMProvider (ABC) · H1.3 ClaudeProvider (dual OAuth/API key) ·
H1.5 cost/usage tracker · H1.10 gestor de concurrencia (CallGate) +
retry+backoff ante 429 (adelanto R3 resiliencia).

Sin SDK pesado: httpx directo a la API de Messages. El modo OAuth (suscripción,
sin pago por consumo) usa Bearer + el beta header oauth, y requiere que el
system prompt empiece con la identidad de Claude Code (confirmado en pruebas
reales 2026-06-10).

Concurrencia: como la cuenta Claude se comparte con Claude Code (dev), cada
llamada saliente pasa por un CallGate (lock entre procesos + espaciado) para
serializar el tráfico de For3s y no chocar con ráfagas externas → evita 429.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from for3s_core.ratelimit import CallGate

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"

# Requisito del modo OAuth-suscripción: el system DEBE empezar con esto.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

# Precios aproximados (USD por millón de tokens) — solo informativo en modo
# OAuth (donde NO se cobra por token, se usa la suscripción).
_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-haiku-4-5": (0.80, 4.0),
}


@dataclass(frozen=True)
class LLMResponse:
    """Respuesta normalizada de un LLM (uniforme sea cual sea el provider)."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def cost_usd(self) -> float:
        """Costo estimado por consumo (referencia; en OAuth real es $0 extra)."""
        pin, pout = _PRICES.get(self.model, (0.0, 0.0))
        return (self.input_tokens * pin + self.output_tokens * pout) / 1_000_000


class LLMProvider(ABC):
    """Interfaz: pedirle a un LLM que responda. (patrón R3/Hermes)."""

    @abstractmethod
    def complete(
        self, user_message: str, *, system: str = "", max_tokens: int = 1024
    ) -> LLMResponse: ...


class ClaudeProvider(LLMProvider):
    """Provider de Claude con soporte DUAL: OAuth-suscripción o API key."""

    def __init__(self, token: str, *, oauth: bool, model: str, timeout: float = 60.0) -> None:
        self._token = token
        self._oauth = oauth
        self._model = model
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        if self._oauth:
            h["authorization"] = f"Bearer {self._token}"
            h["anthropic-beta"] = OAUTH_BETA
        else:
            h["x-api-key"] = self._token
        return h

    def _build_system(self, system: str) -> str:
        """En OAuth, el system DEBE comenzar con la identidad de Claude Code."""
        if self._oauth:
            extra = f"\n\n{system}" if system else ""
            return f"{CLAUDE_CODE_IDENTITY}{extra}"
        return system

    def _post_with_retry(self, payload: dict, *, max_retries: int = 6) -> dict:
        """POST serializado (CallGate) con backoff que respeta retry-after.

        Cada intento toma el carril único de salida; ante 429 espera (usando
        retry-after si la API lo da, si no backoff exponencial) y reintenta.
        """
        delay = 5.0
        for attempt in range(max_retries):
            with CallGate():
                resp = httpx.post(
                    API_URL,
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout,
                )
            if resp.status_code == 429:
                if attempt == max_retries - 1:
                    resp.raise_for_status()
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after else delay
                time.sleep(wait)
                delay = min(delay * 2, 60.0)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("agotados los reintentos por rate limit (429)")

    def complete(
        self, user_message: str, *, system: str = "", max_tokens: int = 1024
    ) -> LLMResponse:
        payload: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_message}],
        }
        full_system = self._build_system(system)
        if full_system:
            payload["system"] = full_system

        data = self._post_with_retry(payload)

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage", {})
        return LLMResponse(
            text=text,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=data.get("model", self._model),
        )
