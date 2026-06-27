"""Gestor de concurrencia LLM de For3s OS — "el repartidor de carriles".

Adelanto de R3 (Token Bucket + circuit breaker) + R5 (cost control). Resuelve
el problema del 429: For3s comparte la cuota de Claude con otros consumidores
(p.ej. Claude Code del propio el dueño), y este gestor reparte la capacidad sin
pasarse, de tres formas combinadas:

  CAPA 1 — Token bucket LOCAL (preventivo): lleva la cuenta de RPM/ITPM/OTPM
           y NO lanza un request si excedería; espera su turno. Así frena
           ANTES de llegar al 429.
  CAPA 2 — Lector de headers (adaptativo): tras cada respuesta, lee
           anthropic-ratelimit-*-remaining y ajusta el ritmo en tiempo real.
  CAPA 3 — Backoff con retry-after (reactivo): si aun así pega 429, respeta
           el retry-after EXACTO del header (no inventa el delay).

  + MODO CORTÉS (Carril A): reserva un margen de capacidad para otros
    consumidores de la misma cuenta (politeness_fraction). For3s solo usa
    hasta (1 - margen) de la cuota → deja aire para Claude Code.

  + SEPARACIÓN POR MODELO: cada modelo (Sonnet/Haiku/Opus) tiene su propio
    bucket, porque Anthropic los limita por separado.

Es process-safe entre workers de For3s vía un lock de archivo simple, para
que varios procesos de For3s no se pisen entre sí.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class RateLimitSnapshot:
    """Lo que sabemos de la cuota tras la última respuesta (headers)."""

    requests_remaining: int | None = None
    input_tokens_remaining: int | None = None
    output_tokens_remaining: int | None = None
    reset_epoch: float | None = None


@dataclass
class _ModelBucket:
    """Token bucket local para UN modelo (RPM + ITPM + OTPM)."""

    rpm: float
    itpm: float
    otpm: float
    politeness_fraction: float  # margen reservado a otros consumidores (0..1)

    _req_tokens: float = field(init=False)
    _in_tokens: float = field(init=False)
    _out_tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: threading.Lock = field(init=False)

    def __post_init__(self) -> None:
        # Capacidad efectiva = límite * (1 - margen cortés)
        f = max(0.0, 1.0 - self.politeness_fraction)
        self._cap_req = max(1.0, self.rpm * f)
        self._cap_in = max(1.0, self.itpm * f)
        self._cap_out = max(1.0, self.otpm * f)
        self._req_tokens = self._cap_req
        self._in_tokens = self._cap_in
        self._out_tokens = self._cap_out
        self._last = 0.0  # se siembra en el primer uso (sin time en init)
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        if self._last == 0.0:
            self._last = now
            return
        elapsed = max(0.0, now - self._last)
        self._last = now
        # rellenado continuo (token bucket): por-minuto → por-segundo
        self._req_tokens = min(self._cap_req, self._req_tokens + self._cap_req * elapsed / 60.0)
        self._in_tokens = min(self._cap_in, self._in_tokens + self._cap_in * elapsed / 60.0)
        self._out_tokens = min(self._cap_out, self._out_tokens + self._cap_out * elapsed / 60.0)

    def wait_time(self, est_in: float, est_out: float, now: float) -> float:
        """Segundos a esperar para tener capacidad (0 si hay ya)."""
        with self._lock:
            self._refill(now)
            waits = [0.0]
            if self._req_tokens < 1.0:
                waits.append((1.0 - self._req_tokens) * 60.0 / self._cap_req)
            if self._in_tokens < est_in:
                waits.append((est_in - self._in_tokens) * 60.0 / self._cap_in)
            if self._out_tokens < est_out:
                waits.append((est_out - self._out_tokens) * 60.0 / self._cap_out)
            return max(waits)

    def consume(self, used_in: float, used_out: float, now: float) -> None:
        with self._lock:
            self._refill(now)
            self._req_tokens -= 1.0
            self._in_tokens -= used_in
            self._out_tokens -= used_out

    def apply_headers(self, snap: RateLimitSnapshot) -> None:
        """Capa 2: si el server dice que queda menos, lo creemos (lo más bajo)."""
        with self._lock:
            if snap.requests_remaining is not None:
                self._req_tokens = min(self._req_tokens, float(snap.requests_remaining))
            if snap.input_tokens_remaining is not None:
                self._in_tokens = min(self._in_tokens, float(snap.input_tokens_remaining))
            if snap.output_tokens_remaining is not None:
                self._out_tokens = min(self._out_tokens, float(snap.output_tokens_remaining))


# Límites por modelo (Tier 1 suscripción aprox; ajustables). Fuente: doc
# anthropic rate-limits. Conservador para Carril A.
_DEFAULT_LIMITS = {
    "claude-sonnet-4-6": (50, 30_000, 8_000),
    "claude-haiku-4-5": (50, 50_000, 10_000),
    "claude-opus-4-7": (50, 500_000, 80_000),
}


class ConcurrencyManager:
    """Reparte la capacidad LLM entre modelos y consumidores (Carril A)."""

    def __init__(
        self,
        *,
        politeness_fraction: float = 0.4,
        limits: dict[str, tuple[int, int, int]] | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self._sleep = sleep
        self._clock = clock
        self._buckets: dict[str, _ModelBucket] = {}
        src = limits or _DEFAULT_LIMITS
        for model, (rpm, itpm, otpm) in src.items():
            self._buckets[model] = _ModelBucket(
                rpm=rpm, itpm=itpm, otpm=otpm, politeness_fraction=politeness_fraction
            )
        self._politeness = politeness_fraction

    def _bucket(self, model: str) -> _ModelBucket:
        if model not in self._buckets:
            rpm, itpm, otpm = _DEFAULT_LIMITS.get(model, (50, 30_000, 8_000))
            self._buckets[model] = _ModelBucket(
                rpm=rpm, itpm=itpm, otpm=otpm, politeness_fraction=self._politeness
            )
        return self._buckets[model]

    # Tope de espera de CAPA 1 (bucket local). ANTES era 600 iteraciones x 5s =
    # hasta 50 MINUTOS bloqueando en silencio (causa del bot "mudo" 2026-06-15:
    # el bucket va a ciegas en OAuth y a veces cree que no hay cuota cuando sí).
    # Ahora: si en MAX_ESPERA_ACQUIRE seg no se libera, DEJA PASAR y que el 429
    # real (con su retry-after y aviso amigable) maneje, en vez de colgar mudo.
    MAX_ESPERA_ACQUIRE = 25.0

    def acquire(self, model: str, *, est_input: int = 2000, est_output: int = 1000) -> None:
        """CAPA 1: espera (cediendo paso) hasta tener capacidad, con tope de tiempo.

        Si el bucket local no libera en MAX_ESPERA_ACQUIRE seg, deja pasar: es
        preferible tocar el 429 real (que avisa) a bloquear minutos en silencio.
        """
        bucket = self._bucket(model)
        t0 = self._clock()
        while self._clock() - t0 < self.MAX_ESPERA_ACQUIRE:
            wait = bucket.wait_time(est_input, est_output, self._clock())
            if wait <= 0.0:
                bucket.consume(est_input, est_output, self._clock())
                return
            self._sleep(min(wait, 2.0))
        # tope alcanzado: deja pasar y que el 429/backoff maneje (con aviso)
        bucket.consume(est_input, est_output, self._clock())

    def report(self, model: str, snap: RateLimitSnapshot) -> None:
        """CAPA 2: alimenta el bucket con lo que reportó el server."""
        self._bucket(model).apply_headers(snap)


def parse_ratelimit_headers(headers) -> RateLimitSnapshot:
    """Extrae anthropic-ratelimit-*-remaining de los headers de respuesta."""

    def _int(name: str) -> int | None:
        v = headers.get(name)
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    return RateLimitSnapshot(
        requests_remaining=_int("anthropic-ratelimit-requests-remaining"),
        input_tokens_remaining=_int("anthropic-ratelimit-input-tokens-remaining"),
        output_tokens_remaining=_int("anthropic-ratelimit-output-tokens-remaining"),
    )


def parse_retry_after(headers, default: float = 5.0) -> float:
    """CAPA 3: lee el retry-after EXACTO del header (segundos)."""
    v = headers.get("retry-after")
    if v is None:
        return default
    try:
        return max(0.0, float(v))
    except (ValueError, TypeError):
        return default
