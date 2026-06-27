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


def test_oauth_system_es_solo_identidad_claude_code() -> None:
    """En OAuth, _build_system devuelve SOLO la identidad Claude Code. BLINDAJE
    429-system (2026-06-22): un system custom NO se concatena (dispararía el
    falso-429) → se ignora a nivel system (el rol va en el user message)."""
    p = ClaudeProvider(token="sk-ant-oat01-x", oauth=True, model="claude-sonnet-4-6")
    # con system vacío → solo identidad
    assert p._build_system("").startswith("You are Claude Code")
    # con system custom → se IGNORA (no se concatena), solo identidad
    system = p._build_system("rol extra que NO debe ir en el system")
    assert system.startswith("You are Claude Code")
    assert "rol extra" not in system  # blindaje: el custom no llega al system


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


class _FakeResp:
    """Respuesta HTTP falsa con un status fijo (para probar el manejo de errores)."""

    def __init__(self, status_code: int, headers: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self.headers: dict = headers or {}
        self.text = text

    def json(self) -> dict:
        return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

    def raise_for_status(self) -> None:
        pass


def test_529_reintenta_y_lanza_sobrecargado(monkeypatch) -> None:
    """El 529 (Anthropic saturado) se reintenta con backoff y, si persiste, lanza
    ServidorSobrecargado (mensaje amable), NO un traceback. Regresión del hueco
    440-448 (2026-06-22)."""
    from for3s_core.llm import ServidorSobrecargado

    llamadas = {"n": 0}
    p = ClaudeProvider(token="t", oauth=True, model="m", sleep=lambda s: None)
    monkeypatch.setattr(p._manager, "acquire", lambda *a, **k: None)

    def siempre_529(payload, betas_extra=()):
        llamadas["n"] += 1
        return _FakeResp(529)

    monkeypatch.setattr(p, "_request_once", siempre_529)
    with pytest.raises(ServidorSobrecargado) as exc:
        p._post({}, est_in=1, est_out=1, max_retries=3)
    assert exc.value.status_code == 529
    assert llamadas["n"] == 3  # reintentó las 3 veces antes de rendirse


def test_529_se_recupera_si_luego_responde(monkeypatch) -> None:
    """Si el 529 es transitorio y el reintento sí responde, NO debe fallar."""
    p = ClaudeProvider(token="t", oauth=True, model="m", sleep=lambda s: None)
    monkeypatch.setattr(p._manager, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(p._manager, "report", lambda *a, **k: None)

    seq = [_FakeResp(529), _FakeResp(200)]

    def primero_529_luego_ok(payload, betas_extra=()):
        return seq.pop(0)

    monkeypatch.setattr(p, "_request_once", primero_529_luego_ok)
    data, _ = p._post({}, est_in=1, est_out=1, max_retries=3)
    assert data["content"][0]["text"] == "ok"  # se recuperó en el 2º intento


def test_429_falso_system_no_reintenta(monkeypatch) -> None:
    """El 429 FALSO (OAuth rechaza system custom: 'Error' sin retry-after) NO se
    reintenta en vano — lanza RateLimitExceeded de una. 2026-06-22."""
    from for3s_core.llm import RateLimitExceeded

    llamadas = {"n": 0}
    p = ClaudeProvider(token="t", oauth=True, model="m", sleep=lambda s: None)
    monkeypatch.setattr(p._manager, "acquire", lambda *a, **k: None)

    def falso_429(payload, betas_extra=()):
        llamadas["n"] += 1
        return _FakeResp(
            429, headers={},
            text='{"type":"error","error":{"type":"rate_limit_error","message":"Error"}}',
        )

    monkeypatch.setattr(p, "_request_once", falso_429)
    with pytest.raises(RateLimitExceeded):
        p._post({}, est_in=1, est_out=1, max_retries=5)
    assert llamadas["n"] == 1  # NO reintentó (sería inútil)


def test_429_real_si_reintenta(monkeypatch) -> None:
    """El 429 REAL (con retry-after) SÍ se reintenta antes de rendirse."""
    from for3s_core.llm import RateLimitExceeded

    llamadas = {"n": 0}
    p = ClaudeProvider(token="t", oauth=True, model="m", sleep=lambda s: None)
    monkeypatch.setattr(p._manager, "acquire", lambda *a, **k: None)

    def real_429(payload, betas_extra=()):
        llamadas["n"] += 1
        return _FakeResp(429, headers={"retry-after": "1"}, text="rate limited")

    monkeypatch.setattr(p, "_request_once", real_429)
    with pytest.raises(RateLimitExceeded):
        p._post({}, est_in=1, est_out=1, max_retries=3)
    assert llamadas["n"] == 3  # sí reintentó las 3 veces
