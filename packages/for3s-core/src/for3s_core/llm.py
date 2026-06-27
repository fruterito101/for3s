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

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from for3s_core.concurrency import (
    ConcurrencyManager,
    parse_ratelimit_headers,
    parse_retry_after,
)

logger = logging.getLogger("for3s.llm")

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
CACHING_BETA = "prompt-caching-2024-07-31"  # Parte C: prompt caching (verificado con OAuth)
PDFS_BETA = "pdfs-2024-09-25"  # multimodal: leer PDFs como bloque document (2026-06-18)

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


class ServidorSobrecargado(Exception):
    """La API de Anthropic devolvió un error transitorio de SU servidor (529/503/
    502/500) y no se recuperó tras reintentar. NO es culpa nuestra ni del cupo —
    es sobrecarga temporal de Anthropic. Mensaje amigable, no traceback."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            "Anthropic está saturado en este momento (error temporal de su lado, "
            f"HTTP {status_code}). No es un problema tuyo ni de tu cupo. "
            "Reintenta en unos momentos."
        )


# Códigos de error TRANSITORIOS de Anthropic que conviene reintentar (no son del
# cliente: 529=overloaded, 503=service unavailable, 502=bad gateway, 500=internal).
_HTTP_TRANSITORIOS = (500, 502, 503, 529)


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

    @property
    def model(self) -> str:
        """El modelo activo (para que /model lo lea/muestre)."""
        return self._model

    def set_model(self, model: str) -> None:
        """Cambia el modelo en caliente (lo usa el /model de For3s). Mantiene el
        manager de concurrencia y todo lo demás — solo cambia a qué modelo apunta."""
        self._model = model

    def _headers(self, *, betas_extra: tuple[str, ...] = ()) -> dict[str, str]:
        h = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        # Parte C: el beta de caching va SIEMPRE (inofensivo si el payload no
        # usa cache_control; necesario cuando sí). En OAuth se combina con su beta.
        # betas_extra: betas puntuales por request (ej. PDFs multimodales).
        betas = [CACHING_BETA, *betas_extra]
        if self._oauth:
            h["authorization"] = f"Bearer {self._token}"
            betas = [OAUTH_BETA, *betas]
        else:
            h["x-api-key"] = self._token
        h["anthropic-beta"] = ",".join(betas)
        return h

    def _build_system(self, system: str) -> str:
        if self._oauth:
            # BLINDAJE 429-system (2026-06-22): el OAuth de suscripción RECHAZA
            # con un falso-429 cualquier system prompt custom. Por eso, en OAuth, el
            # system SIEMPRE debe ser solo la identidad de Claude Code; el rol/contexto
            # va en el USER message (así lo hacen TODOS los flujos). Si un flujo futuro
            # se equivoca y pasa un system NO vacío, NO lo concatenamos (eso dispararía
            # el 429) → lo ignoramos a nivel system y AVISAMOS en el log para cazarlo.
            # Última línea de defensa: degrada en vez de romper.
            if system:
                logger.error(
                    "[429-GUARD] se pasó un system custom en modo OAuth (%d chars) → "
                    "lo ignoro para no disparar el falso-429. El flujo que llama debe "
                    "poner sus instrucciones en el USER message, no en system. "
                    "system[:80]=%r", len(system), system[:80],
                )
            return CLAUDE_CODE_IDENTITY
        return system

    def _request_once(self, payload: dict, betas_extra: tuple[str, ...] = ()) -> httpx.Response:
        return httpx.post(
            API_URL, headers=self._headers(betas_extra=betas_extra),
            json=payload, timeout=self._timeout,
        )

    def _post(self, payload: dict, *, est_in: int, est_out: int, max_retries: int = 5,
              betas_extra: tuple[str, ...] = ()):
        """CAPA 1 (acquire) + CAPA 3 (backoff retry-after) + CAPA 2 (report headers)."""
        for attempt in range(max_retries):
            # CAPA 1: pide turno al gestor (cede paso si no hay cuota)
            self._manager.acquire(self._model, est_input=est_in, est_output=est_out)

            resp = self._request_once(payload, betas_extra)

            if resp.status_code == 429:
                # DIAGNÓSTICO (2026-06-22): hay DOS tipos de 429 que conviene
                # distinguir (antes se trataban igual y confundían el debug):
                #   (a) 429 REAL de rate-limit → trae header retry-after → esperar sirve.
                #   (b) 429 FALSO = el OAuth de suscripción RECHAZA un system prompt
                #       custom → mensaje "Error" sin retry-after → reintentar es INÚTIL;
                #       el fix es poner el rol en el user message y system="" (ver H6).
                tiene_retry = resp.headers.get("retry-after") is not None
                cuerpo = (resp.text or "")[:120]
                if not tiene_retry and '"message":"Error"' in cuerpo:
                    logger.error(
                        "[429-SYSTEM] OAuth rechazó un system prompt custom (NO es "
                        "rate-limit real, sin retry-after). Revisar que el rol vaya en "
                        "el user message y system='' en este flujo. cuerpo=%s", cuerpo,
                    )
                    raise RateLimitExceeded(0)  # reintentar no ayuda → aviso amigable
                # CAPA 3: 429 real → respeta el retry-after exacto
                wait = parse_retry_after(resp.headers)
                logger.warning(
                    "[429-RATE] rate-limit real (retry-after=%.0fs, intento %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                if wait > MAX_WAIT_SECONDS:
                    raise RateLimitExceeded(wait)
                if attempt < max_retries - 1:
                    self._sleep(wait)
                    continue
                # Agotados los reintentos por 429 → mensaje AMIGABLE, no traceback.
                raise RateLimitExceeded(wait)

            if resp.status_code in _HTTP_TRANSITORIOS:
                # Error TRANSITORIO de Anthropic (529 overloaded, 503, 502, 500).
                # NO es del cliente ni del cupo → reintentar con BACKOFF exponencial
                # (estos errores NO traen retry-after). Antes esto reventaba en
                # raise_for_status() y dejaba al bot MUDO (hueco 440-448, 2026-06-22).
                if attempt < max_retries - 1:
                    espera = min(2.0 * (2 ** attempt), MAX_WAIT_SECONDS)  # 2,4,8,16…
                    self._sleep(espera)
                    continue
                # Agotados los reintentos → mensaje amigable, no traceback.
                raise ServidorSobrecargado(resp.status_code)

            resp.raise_for_status()
            # CAPA 2: aprende de los headers cuánta cuota queda
            self._manager.report(self._model, parse_ratelimit_headers(resp.headers))
            return resp.json(), resp.headers

        raise RateLimitExceeded(parse_retry_after({}))

    def complete(
        self, user_message: str, *, system: str = "", max_tokens: int = 1024,
        adjuntos: list[dict] | None = None,
    ) -> LLMResponse:
        # adjuntos: bloques multimodales (imagen/document/texto extraído) que
        # acompañan al mensaje (2026-06-18). Si vienen, el content deja de
        # ser un string y pasa a ser una LISTA de bloques: primero el texto del
        # usuario, luego los adjuntos. Si no, todo sigue igual (texto puro).
        if adjuntos:
            content: list[dict] = [{"type": "text", "text": user_message}]
            content.extend(adjuntos)
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": user_message}]
        payload: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        full_system = self._build_system(system)
        if full_system:
            payload["system"] = full_system

        # Si algún adjunto es un PDF (bloque document), la API exige el beta de
        # PDFs. Inofensivo si no hay PDF; solo lo añadimos cuando lo hay.
        betas_extra: tuple[str, ...] = ()
        if adjuntos and any(b.get("type") == "document" for b in adjuntos):
            betas_extra = (PDFS_BETA,)

        # estimación gruesa de tokens para el gestor (R3 refinará con tokenizer).
        # Los adjuntos en base64 pesan; sumamos su largo para que el bucket no
        # subestime y no topemos el rate-limit.
        peso_adj = sum(len(str(b)) for b in adjuntos) // 3 if adjuntos else 0
        est_in = max(100, len(user_message) // 3 + len(full_system) // 3 + peso_adj)
        data, resp_headers = self._post(
            payload, est_in=est_in, est_out=max_tokens, betas_extra=betas_extra,
        )

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
        # Parte C: PROMPT CACHING. system y tools se reenvían IDÉNTICOS en cada
        # vuelta del loop → marcarlos cacheables reduce 75-90% el input contado
        # (verificado con OAuth: cache_creation 1ª vez, cache_read después).
        if full_system:
            # system como bloque de texto con cache_control (si supera el mínimo
            # de ~1024 tokens, Anthropic lo cachea; si no, lo ignora sin error).
            payload["system"] = [
                {
                    "type": "text",
                    "text": full_system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if tools:
            # marcar el ÚLTIMO tool con cache_control → cachea TODO el array de
            # schemas (lo más pesado del payload). Copia para no mutar el original.
            tools_cache = [dict(t) for t in tools]
            tools_cache[-1] = {**tools_cache[-1], "cache_control": {"type": "ephemeral"}}
            payload["tools"] = tools_cache
        # H-F: tool_choice fuerza a usar tool (ej. {"type":"any"}) — evita que
        # el modelo narre/invente en vez de ejecutar. Solo si se pide y hay tools.
        if tool_choice and tools:
            payload["tool_choice"] = tool_choice

        # estimación de tokens para el gestor de concurrencia (Parte A anti-RL).
        # CLAVE: incluir el peso de los SCHEMAS de tools — pesan ~6-10k chars y
        # se reenvían en CADA vuelta del loop. Antes solo se contaba messages →
        # el bucket subestimaba → topaba el 429 (rate-limit por-minuto del
        # tool-use). Ahora el bucket conoce el peso real → espacia bien.
        approx = sum(len(str(m.get("content", ""))) for m in messages) // 3
        tools_chars = len(str(tools)) if tools else 0
        est_in = max(200, approx + len(full_system) // 3 + tools_chars // 3)
        data, resp_headers = self._post(payload, est_in=est_in, est_out=max_tokens)
        return data, resp_headers
