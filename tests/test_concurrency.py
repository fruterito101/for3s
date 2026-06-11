"""Tests del gestor de concurrencia (H1.10) — sin red, con reloj/sleep falsos."""

from __future__ import annotations

from for3s_core.concurrency import (
    ConcurrencyManager,
    RateLimitSnapshot,
    parse_ratelimit_headers,
    parse_retry_after,
)


class FakeClock:
    """Reloj controlable + sleep que avanza el reloj (para tests deterministas)."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.slept += secs
        self.t += secs


def test_acquire_sin_espera_cuando_hay_cuota() -> None:
    clk = FakeClock()
    mgr = ConcurrencyManager(politeness_fraction=0.0, sleep=clk.sleep, clock=clk.now)
    mgr.acquire("claude-sonnet-4-6", est_input=100, est_output=100)
    assert clk.slept == 0.0  # primera llamada: hay cuota, no espera


def test_modo_cortes_reduce_capacidad() -> None:
    # con 40% de margen cortés, Sonnet (50 rpm) deja ~30 para For3s
    clk = FakeClock()
    mgr = ConcurrencyManager(politeness_fraction=0.4, sleep=clk.sleep, clock=clk.now)
    # consumir muchos requests seguidos sin avanzar el reloj → debe empezar a esperar
    espera_total_antes = clk.slept
    for _ in range(40):
        mgr.acquire("claude-sonnet-4-6", est_input=10, est_output=10)
    assert clk.slept > espera_total_antes  # tuvo que ceder paso (cortés)


def test_separacion_por_modelo() -> None:
    clk = FakeClock()
    mgr = ConcurrencyManager(politeness_fraction=0.0, sleep=clk.sleep, clock=clk.now)
    # agotar sonnet no debe afectar a haiku (límites separados)
    for _ in range(10):
        mgr.acquire("claude-sonnet-4-6", est_input=10, est_output=10)
    slept_antes = clk.slept
    mgr.acquire("claude-haiku-4-5", est_input=10, est_output=10)
    assert clk.slept == slept_antes  # haiku tiene su propio bucket, no esperó


def test_report_headers_baja_la_cuota() -> None:
    clk = FakeClock()
    mgr = ConcurrencyManager(politeness_fraction=0.0, sleep=clk.sleep, clock=clk.now)
    mgr.acquire("claude-sonnet-4-6", est_input=10, est_output=10)
    # el server dice: solo te quedan 0 requests
    mgr.report("claude-sonnet-4-6", RateLimitSnapshot(requests_remaining=0))
    # el siguiente acquire debe esperar (capa 2 aplicó el header)
    slept_antes = clk.slept
    mgr.acquire("claude-sonnet-4-6", est_input=10, est_output=10)
    assert clk.slept > slept_antes


def test_parse_ratelimit_headers() -> None:
    snap = parse_ratelimit_headers(
        {
            "anthropic-ratelimit-requests-remaining": "12",
            "anthropic-ratelimit-input-tokens-remaining": "5000",
            "anthropic-ratelimit-output-tokens-remaining": "900",
        }
    )
    assert snap.requests_remaining == 12
    assert snap.input_tokens_remaining == 5000
    assert snap.output_tokens_remaining == 900


def test_parse_retry_after() -> None:
    assert parse_retry_after({"retry-after": "7"}) == 7.0
    assert parse_retry_after({}, default=3.0) == 3.0
    assert parse_retry_after({"retry-after": "basura"}, default=2.0) == 2.0
