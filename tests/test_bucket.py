"""Tests directos del token bucket (E3) — la lógica fina sin probar de concurrency.py.

El ConcurrencyManager ya tenía tests; faltaba probar el corazón: _ModelBucket
(_refill con el tiempo, wait_time, consume, apply_headers). Esto sube la
confianza del archivo más débil sin reescribir código que ya funciona.
"""

from __future__ import annotations

from for3s_core.concurrency import RateLimitSnapshot, _ModelBucket


def _bucket(rpm=60.0, itpm=6000.0, otpm=600.0, polite=0.0) -> _ModelBucket:
    return _ModelBucket(rpm=rpm, itpm=itpm, otpm=otpm, politeness_fraction=polite)


def test_bucket_lleno_no_espera() -> None:
    b = _bucket()
    # primer uso siembra el reloj; con bucket lleno no hay espera
    assert b.wait_time(est_in=100, est_out=50, now=1000.0) == 0.0


def test_politeness_reduce_capacidad() -> None:
    # con 50% de margen cortés, la capacidad efectiva baja a la mitad
    pleno = _bucket(itpm=6000, polite=0.0)
    cortes = _bucket(itpm=6000, polite=0.5)
    assert cortes._cap_in == pleno._cap_in / 2


def test_consume_baja_tokens_y_obliga_espera() -> None:
    b = _bucket(rpm=60, itpm=6000, otpm=600)
    b.wait_time(0, 0, now=1000.0)  # siembra reloj
    # consumir casi todo el input
    b.consume(used_in=5999, used_out=10, now=1000.0)
    # pedir más de lo que queda → debe pedir esperar (>0)
    assert b.wait_time(est_in=500, est_out=0, now=1000.0) > 0.0


def test_refill_recupera_con_el_tiempo() -> None:
    b = _bucket(rpm=60, itpm=6000, otpm=600)
    b.wait_time(0, 0, now=1000.0)  # siembra
    b.consume(used_in=6000, used_out=0, now=1000.0)  # vacía el input
    # 60s después (1 minuto) el bucket de input se rellena completo
    assert b.wait_time(est_in=6000, est_out=0, now=1060.0) == 0.0


def test_apply_headers_baja_a_lo_que_dice_el_server() -> None:
    b = _bucket(rpm=60, itpm=6000, otpm=600)
    b.wait_time(0, 0, now=1000.0)
    # el server dice que solo quedan 10 input tokens → le creemos
    b.apply_headers(
        RateLimitSnapshot(
            requests_remaining=None, input_tokens_remaining=10, output_tokens_remaining=None
        )
    )
    assert b.wait_time(est_in=500, est_out=0, now=1000.0) > 0.0


def test_apply_headers_no_sube_capacidad() -> None:
    # apply_headers solo BAJA (se cree lo más restrictivo), nunca sube
    b = _bucket(itpm=6000)
    b.wait_time(0, 0, now=1000.0)
    b.apply_headers(
        RateLimitSnapshot(
            requests_remaining=None, input_tokens_remaining=999999, output_tokens_remaining=None
        )
    )
    # sigue limitado por su capacidad real, no por el header inflado
    assert b._in_tokens <= b._cap_in
