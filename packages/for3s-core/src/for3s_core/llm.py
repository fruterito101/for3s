"""Capa LLM de For3s OS — el motor generativo (Nodo 3 PFC, versión H1).

H1.2 LLMProvider (ABC) · H1.3 ClaudeProvider (dual OAuth/API key) ·
H1.5 cost/usage tracker · H1.10 gestor de concurrencia 3 capas (R3 adelanto).

Sin SDK pesado: httpx directo a la API de Messages. El modo OAuth (suscripción,
sin pago por consumo) usa Bearer + el beta header oauth, y requiere que el
system prompt empiece con la identidad de Claude Code (confirmado 2026-06-10).

El gestor de concurrencia (ConcurrencyManager) reparte la cuota para no pegar
429 cuando For3s comparte la cuenta con otros consumidores (Carril A).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from for3s_core.concurrency import (
    ConcurrencyManager,
    parse_ratelimit_headers,
    parse_retry_after,
)

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"

CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

# Si el retry-after supera esto (seg), NO esperamos: avisamos al usuario.
MAX_WAIT_SECONDS = 60


class RateLimitExceeded(Exception):
    """La cuenta topó su cupo y el retry-after es demasiado largo."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        mins = int(retry_after // 60)
        super().__init__(
            f"La cuenta de Claude topó su cupo. Reinicia en ~{mins} min. "
            "Usa otra cuenta/API key o intenta más tarde."
        )


_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-haiku-4-5": (0.80, 4.0),
}


@dataclass(frozen=True)
class LLMResponse:
    """Respuesta normalizada de un LLM."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def cost_usd(self) -> float:
        pin, pout = _PRICES.get(self.model, (0.0, 0.0))
        return (self.input_tokens * pin + self.output_tokens * pout) / 1_000_000


class LLMProvider(ABC):
    """Interfaz: pedirle a un LLM que responda."""

    @abstractmethod
    def complete(
        self, user_message: str, *, system: str = "", max_tokens: int = 1024
    ) -> LLMResponse: ...


class ClaudeProvider(LLMProvider):
    """Provider de Claude con soporte DUAL (OAuth/API key) + gestor de concurrencia."""

    def __init__(
        self,
        token: str,
        *,
        oauth: bool,
        model: str,
        timeout: float = 60.0,
        manager: ConcurrencyManager | None = None,
        sleep=time.sleep,
    ) -> None:
        self._token = token
        self._oauth = oauth
        self._model = model
        self._timeout = timeout
        self._manager = manager or ConcurrencyManager()
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        h = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        if self._oauth:
            h["authorization"] = f"Bearer {self._token}"
            h["anthropic-beta"] = OAUTH_BETA
        else:
            h["x-api-key"] = self._token
        return h

    def _build_system(self, system: str) -> str:
        if self._oauth:
            extra = f"\n\n{system}" if system else ""
            return f"{CLAUDE_CODE_IDENTITY}{extra}"
        return system

    def _request_once(self, payload: dict) -> httpx.Response:
        return httpx.post(API_URL, headers=self._headers(), json=payload, timeout=self._timeout)

    def _post(self, payload: dict, *, est_in: int, est_out: int, max_retries: int = 5) -> dict:
        """CAPA 1 (acquire) + CAPA 3 (backoff retry-after) + CAPA 2 (report headers)."""
        for attempt in range(max_retries):
            # CAPA 1: pide turno al gestor (cede paso si no hay cuota)
            self._manager.acquire(self._model, est_input=est_in, est_output=est_out)

            resp = self._request_once(payload)

            if resp.status_code == 429:
                # CAPA 3: respeta el retry-after exacto
                wait = parse_retry_after(resp.headers)
                if wait > MAX_WAIT_SECONDS:
                    raise RateLimitExceeded(wait)
                if attempt < max_retries - 1:
                    self._sleep(wait)
                    continue
                resp.raise_for_status()

            resp.raise_for_status()
            # CAPA 2: aprende de los headers cuánta cuota queda
            self._manager.report(self._model, parse_ratelimit_headers(resp.headers))
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

        # estimación gruesa de tokens para el gestor (R3 refinará con tokenizer)
        est_in = max(100, len(user_message) // 3 + len(full_system) // 3)
        data = self._post(payload, est_in=est_in, est_out=max_tokens)

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
