"""Gestor de concurrencia y rate de For3s OS (adelanto de R3 B3).

Problema real (detectado en H1, 2026-06-10): la cuenta Claude se comparte
entre Claude Code (dev) y For3s OS. Llamadas concurrentes al mismo token →
429. Solución: serializar el tráfico SALIENTE de For3s con un lock entre
procesos + espaciado mínimo entre llamadas, de modo que For3s nunca dispare
ráfagas que choquen con Claude Code.

Esto NO es el token bucket completo de R3/H7 (per-workspace, distribuido);
es el cimiento mínimo: un "carril único y ordenado" para las salidas a la API.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_LOCK_PATH = Path(os.environ.get("FOR3S_LLM_LOCK", "/tmp/for3s-llm.lock"))
_STAMP_PATH = Path(os.environ.get("FOR3S_LLM_STAMP", "/tmp/for3s-llm.stamp"))

# Espaciado mínimo entre llamadas salientes de For3s (segundos). Da aire al
# rate compartido con Claude Code.
MIN_INTERVAL_S = float(os.environ.get("FOR3S_LLM_MIN_INTERVAL", "3.0"))


class CallGate:
    """Carril único para salidas a la API: lock entre procesos + espaciado.

    Uso:
        with CallGate():
            ... una sola llamada a la vez en toda la máquina ...
    """

    def __init__(self, *, timeout_s: float = 120.0, poll_s: float = 0.25) -> None:
        self._timeout = timeout_s
        self._poll = poll_s
        self._fd: int | None = None

    def __enter__(self) -> CallGate:
        self._acquire()
        self._respect_spacing()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stamp_now()
        self._release()

    def _acquire(self) -> None:
        """Lock exclusivo entre procesos vía O_CREAT|O_EXCL (atómico)."""
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return
            except FileExistsError:
                # ¿lock huérfano? si tiene > timeout, lo rompemos.
                try:
                    age = time.time() - _LOCK_PATH.stat().st_mtime
                    if age > self._timeout:
                        _LOCK_PATH.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError("no se pudo adquirir el CallGate (lock ocupado)") from None
                time.sleep(self._poll)

    def _release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        _LOCK_PATH.unlink(missing_ok=True)

    def _respect_spacing(self) -> None:
        """Espera para mantener al menos MIN_INTERVAL_S desde la última llamada."""
        try:
            last = _STAMP_PATH.stat().st_mtime
        except FileNotFoundError:
            return
        wait = MIN_INTERVAL_S - (time.time() - last)
        if wait > 0:
            time.sleep(wait)

    def _stamp_now(self) -> None:
        _STAMP_PATH.touch()
