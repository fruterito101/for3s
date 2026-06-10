"""Test del CallGate (H1.10) — el carril único de salida a la API."""

from __future__ import annotations

import time
from pathlib import Path

from for3s_core.ratelimit import CallGate


def test_gate_serializa(tmp_path: Path, monkeypatch) -> None:
    """Dos CallGate seguidos no se solapan (el lock se libera y re-adquiere)."""
    monkeypatch.setenv("FOR3S_LLM_LOCK", str(tmp_path / "l.lock"))
    monkeypatch.setenv("FOR3S_LLM_STAMP", str(tmp_path / "l.stamp"))
    # recargar módulo para tomar los paths de env nuevos
    import importlib

    from for3s_core import ratelimit

    importlib.reload(ratelimit)

    with ratelimit.CallGate():
        assert (tmp_path / "l.lock").exists()
    # tras salir, el lock se liberó
    assert not (tmp_path / "l.lock").exists()


def test_gate_es_reentrante_secuencial() -> None:
    """Adquirir, soltar, volver a adquirir funciona (no deja lock huérfano)."""
    with CallGate(timeout_s=5):
        pass
    t0 = time.monotonic()
    with CallGate(timeout_s=5):
        pass
    assert time.monotonic() - t0 < 5
