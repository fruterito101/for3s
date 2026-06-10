"""Agente mínimo de For3s OS (H1) — arma el prompt y llama al LLM.

H1.4 prompt builder mínimo. En H10+ esto se vuelve el PFC con planning y
confidence; en H1 es solo: tomar el mensaje del usuario, dar un poco de
contexto de rol, y devolver la respuesta de Claude.
"""

from __future__ import annotations

from for3s_core.llm import LLMProvider, LLMResponse

# Rol mínimo de For3s (en modo OAuth se concatena DESPUÉS de la identidad
# Claude Code, que el provider añade automáticamente).
FOR3S_ROLE = (
    "Además, actúas como For3s OS, un asistente de análisis de código y QA. "
    "Responde en español, claro y directo. Si ves un bug, dilo explícito."
)


class Agent:
    """El agente H1: una sola pieza, sin memoria todavía."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def ask(self, message: str, *, max_tokens: int = 1024) -> LLMResponse:
        return self._provider.complete(message, system=FOR3S_ROLE, max_tokens=max_tokens)
