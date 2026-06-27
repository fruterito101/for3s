"""Tests de H2 — audit hash chain (lógica pura) + agente con historial.

La parte de Postgres se valida en el DEMO end-to-end (requiere DB viva).
Aquí probamos la lógica determinista: el encadenamiento de hashes y que
el agente arma bien el prompt con historial.
"""

from __future__ import annotations

from for3s_core import audit
from for3s_core.agent import Agent
from for3s_core.llm import LLMProvider, LLMResponse


class FakeProvider(LLMProvider):
    def __init__(self) -> None:
        self.last_prompt = ""

    def complete(
        self, user_message: str, *, system: str = "", max_tokens: int = 1024,
        adjuntos: list[dict] | None = None, **kwargs,
    ) -> LLMResponse:
        # adjuntos/**kwargs: el provider real ganó params opcionales (multimodal,
        # 2026-06-18); el fake los acepta y los ignora para no romper el test.
        self.last_prompt = user_message
        self.last_adjuntos = adjuntos
        return LLMResponse(text="ok", input_tokens=1, output_tokens=1, model="m")


def test_hash_es_deterministico() -> None:
    h1 = audit.compute_hash(
        audit.GENESIS_HASH, "2026-01-01T00:00:00+00:00", "default", "user", "x", {}
    )
    h2 = audit.compute_hash(
        audit.GENESIS_HASH, "2026-01-01T00:00:00+00:00", "default", "user", "x", {}
    )
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_cambia_si_cambia_contenido() -> None:
    base = audit.compute_hash(
        audit.GENESIS_HASH, "2026-01-01T00:00:00+00:00", "default", "user", "x", {}
    )
    distinto = audit.compute_hash(
        audit.GENESIS_HASH, "2026-01-01T00:00:00+00:00", "default", "user", "y", {}
    )
    assert base != distinto


def test_cadena_encadena() -> None:
    # el hash_self de una entrada alimenta el hash_prev de la siguiente
    h1 = audit.compute_hash(audit.GENESIS_HASH, "t1", "default", "user", "a", {})
    h2 = audit.compute_hash(h1, "t2", "default", "for3s", "b", {})
    # si alteramos h1, h2 recomputado ya no cuadra
    h1_alterado = audit.compute_hash(audit.GENESIS_HASH, "t1", "default", "user", "ALTERADO", {})
    h2_con_alterado = audit.compute_hash(h1_alterado, "t2", "default", "for3s", "b", {})
    assert h2 != h2_con_alterado  # manipulación detectable


def test_agente_arma_historial() -> None:
    fake = FakeProvider()
    agent = Agent(fake)
    history = [
        {"role": "user", "content": "hola, me llamo el dueño"},
        {"role": "assistant", "content": "hola el dueño"},
        {"role": "user", "content": "¿cómo me llamo?"},
    ]
    agent.ask_with_history(history)
    # el prompt debe incluir el contexto previo (la memoria)
    assert "el dueño" in fake.last_prompt
    assert "¿cómo me llamo?" in fake.last_prompt
