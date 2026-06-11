"""Agente mínimo de For3s OS (H1) — arma el prompt y llama al LLM.

H1.4 prompt builder mínimo. En modo OAuth (suscripción) el system DEBE ser
solo la identidad de Claude Code (Anthropic rechaza system custom con 429),
así que el rol de For3s se antepone al MENSAJE del usuario. En modo API key
el rol va en el system, como es natural. En H10+ esto se vuelve el PFC.
"""

from __future__ import annotations

from for3s_core.llm import ClaudeProvider, LLMProvider, LLMResponse

FOR3S_ROLE = (
    "Actúas como For3s OS, un asistente de análisis de código y QA. "
    "Responde en español, claro y directo. Si ves un bug, dilo explícito."
)


class Agent:
    """El agente H1: una sola pieza, sin memoria todavía."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        # ¿el provider está en modo OAuth-suscripción? (no admite system custom)
        self._oauth = isinstance(provider, ClaudeProvider) and getattr(provider, "_oauth", False)

    def ask(self, message: str, *, max_tokens: int = 1024) -> LLMResponse:
        if self._oauth:
            # El rol va en el mensaje, no en el system (la suscripción lo exige).
            prompt = f"[{FOR3S_ROLE}]\n\n{message}"
            return self._provider.complete(prompt, system="", max_tokens=max_tokens)
        return self._provider.complete(message, system=FOR3S_ROLE, max_tokens=max_tokens)
