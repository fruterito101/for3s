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
    usage_5h: float | None = None  # cupo suscripción usado en 5h (0..1)
    usage_7d: float | None = None  # cupo suscripción usado en 7d (0..1)

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

    def _post(self, payload: dict, *, est_in: int, est_out: int, max_retries: int = 5):
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
                # Agotados los reintentos por 429 → mensaje AMIGABLE, no traceback.
                # El tool-use con suscripción topa el rate-limit instantáneo más
                # rápido (payloads grandes); cuando insiste, avisamos con gracia.
                raise RateLimitExceeded(wait)

            resp.raise_for_status()
            # CAPA 2: aprende de los headers cuánta cuota queda
            self._manager.report(self._model, parse_ratelimit_headers(resp.headers))
            return resp.json(), resp.headers

        raise RateLimitExceeded(parse_retry_after({}))

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
        data, resp_headers = self._post(payload, est_in=est_in, est_out=max_tokens)

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage", {})

        def _pct(name: str) -> float | None:
            v = resp_headers.get(name)
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        return LLMResponse(
            text=text,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=data.get("model", self._model),
            usage_5h=_pct("anthropic-ratelimit-unified-5h-utilization"),
            usage_7d=_pct("anthropic-ratelimit-unified-7d-utilization"),
        )

    def complete_with_tools(
        self,
        messages: list[dict],
        *,
        system: str = "",
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        tool_choice: dict | None = None,
    ) -> tuple[dict, dict]:
        """Una vuelta del loop de tool-use (Paso 3 migración GitHub→MCP).

        A diferencia de complete() (1 user_message, solo texto), recibe el
        historial messages[] COMPLETO (incluyendo bloques tool_use/tool_result)
        y la lista de `tools`. Devuelve (data_cruda, headers) — el LOOP de arriba
        inspecciona stop_reason y los bloques tool_use. NO aplana a texto: eso
        lo decide el caller.

        Reusa toda la robustez de _post (concurrencia + retry-after + headers).
        """
        payload: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        full_system = self._build_system(system)
        if full_system:
            payload["system"] = full_system
        if tools:
            payload["tools"] = tools
        # H-F: tool_choice fuerza a usar tool (ej. {"type":"any"}) — evita que
        # el modelo narre/invente en vez de ejecutar. Solo si se pide y hay tools.
        if tool_choice and tools:
            payload["tool_choice"] = tool_choice

        # estimación gruesa para el gestor de concurrencia
        approx = sum(len(str(m.get("content", ""))) for m in messages) // 3
        est_in = max(200, approx + len(full_system) // 3)
        data, resp_headers = self._post(payload, est_in=est_in, est_out=max_tokens)
        return data, resp_headers
