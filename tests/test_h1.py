"""Tests de H1 — provider mockeado + cost tracker (sin tocar la red real)."""

from __future__ import annotations

import pytest
from for3s_core.agent import Agent
from for3s_core.llm import ClaudeProvider, LLMProvider, LLMResponse


class FakeProvider(LLMProvider):
    """Provider falso para probar el agente sin llamar a Claude."""

    def __init__(self) -> None:
        self.last_system = ""

    def complete(
        self, user_message: str, *, system: str = "", max_tokens: int = 1024
    ) -> LLMResponse:
        self.last_system = system
        return LLMResponse(
            text=f"eco: {user_message}",
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
        )


def test_agent_responde() -> None:
    agent = Agent(FakeProvider())
    resp = agent.ask("hola")
    assert resp.text == "eco: hola"


def test_agent_pasa_rol_for3s() -> None:
    fake = FakeProvider()
    Agent(fake).ask("x")
    assert "For3s OS" in fake.last_system


def test_cost_sonnet() -> None:
    r = LLMResponse(
        text="", input_tokens=1_000_000, output_tokens=1_000_000, model="claude-sonnet-4-6"
    )
    # 3 USD in + 15 USD out por millón = 18.0
    assert r.cost_usd == pytest.approx(18.0)


def test_oauth_inyecta_identidad_claude_code() -> None:
    p = ClaudeProvider(token="sk-ant-oat01-x", oauth=True, model="claude-sonnet-4-6")
    system = p._build_system("rol extra")
    assert system.startswith("You are Claude Code")
    assert "rol extra" in system


def test_apikey_no_inyecta_identidad() -> None:
    p = ClaudeProvider(token="sk-ant-api03-x", oauth=False, model="claude-sonnet-4-6")
    assert p._build_system("solo esto") == "solo esto"


def test_headers_oauth_vs_apikey() -> None:
    oauth = ClaudeProvider(token="t", oauth=True, model="m")._headers()
    assert oauth["authorization"] == "Bearer t"
    assert "anthropic-beta" in oauth
    apikey = ClaudeProvider(token="t", oauth=False, model="m")._headers()
    assert apikey["x-api-key"] == "t"
    assert "authorization" not in apikey
