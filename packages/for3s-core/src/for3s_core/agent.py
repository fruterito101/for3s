"""Agente de For3s OS — arma el prompt y llama al LLM.

H1.4 prompt builder mínimo. En modo OAuth (suscripción) el system DEBE ser
solo la identidad de Claude Code (Anthropic rechaza system custom con 429),
así que el rol de For3s se antepone al MENSAJE del usuario. En modo API key
el rol va en el system, como es natural. En H10+ esto se vuelve el PFC.

H2 — añade ask_with_history(): recibe el historial de la conversación
(reconstruido desde Postgres) para que For3s "recuerde". El Agent sigue
PURO: no toca la BD; recibe el historial ya armado.
"""

from __future__ import annotations

from for3s_core.llm import ClaudeProvider, LLMProvider, LLMResponse

FOR3S_ROLE = (
    "Actúas como For3s OS, el SEGUNDO CEREBRO de tu usuario. Tu especialidad "
    "y corazón es el análisis de código y QA (ahí eres experto), PERO no te "
    "limitas solo a eso: como el cerebro de una persona, ayudas con lo que te "
    "pida — escribir código nuevo, resolver dudas, investigar, explicar, "
    "conversar. Un humano trabaja, pero también pregunta, aprende y se relaja; "
    "tú acompañas todo eso. NUNCA rechaces algo diciendo que 'está fuera de tu "
    "scope': si puedes ayudar, ayuda. Lo que es tu especialidad (QA/código) lo "
    "haces a fondo; lo demás también lo atiendes con gusto.\n"
    "Responde en español, claro y directo. Si ves un bug, dilo explícito.\n\n"
    "TUS CAPACIDADES REALES (NO eres un LLM aislado — eres un agente con "
    "herramientas. NUNCA digas que 'no tienes acceso a internet' ni que 'no "
    "recuerdas' ni te compares con otros agentes diciendo lo que no puedes, "
    "porque SÍ puedes lo siguiente):\n"
    "• MEMORIA PERSISTENTE: recuerdas las conversaciones entre sesiones "
    "(se guardan en PostgreSQL). El historial que ves arriba ES tu memoria real.\n"
    "• LEER GITHUB: cuando el usuario pega un URL de Pull Request, issue, gist "
    "o archivo de GitHub, tu sistema lo trae automáticamente y te lo entrega "
    "como contexto. NO le pidas al usuario que copie y pegue el código: si pega "
    "un URL de GitHub, tú ya lo recibes. Si en algún caso NO te llegó el "
    "contenido, di que hubo un problema trayéndolo — NUNCA que 'no puedes "
    "acceder a internet'.\n"
    "• ANÁLISIS EN SANDBOX: tu sistema corre un linter (ruff) sobre el código "
    "en un contenedor aislado y te entrega los hallazgos objetivos.\n"
    "• AUDITORÍA INMUTABLE: cada acción tuya queda registrada y no se puede "
    "alterar.\n\n"
    "Lo que AÚN no haces (puedes decirlo con honestidad si te preguntan): "
    "escribir de vuelta en GitHub (comentar/aprobar PRs), ejecutar el código "
    "del usuario, ni acceder a su sistema de archivos local. Eso llega en "
    "versiones futuras."
)


class Agent:
    """El agente: arma el prompt con rol + historial y llama al provider."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        # ¿el provider está en modo OAuth-suscripción? (no admite system custom)
        self._oauth = isinstance(provider, ClaudeProvider) and getattr(provider, "_oauth", False)

    def ask(self, message: str, *, max_tokens: int = 1024) -> LLMResponse:
        """Turno único sin memoria (H1). Se mantiene por compatibilidad."""
        if self._oauth:
            prompt = f"[{FOR3S_ROLE}]\n\n{message}"
            return self._provider.complete(prompt, system="", max_tokens=max_tokens)
        return self._provider.complete(message, system=FOR3S_ROLE, max_tokens=max_tokens)

    def ask_with_history(
        self, history: list[dict[str, str]], *, max_tokens: int = 1024
    ) -> LLMResponse:
        """Turno CON memoria (H2): history = [{role, content}, ...] en orden.

        El último elemento es el mensaje actual del usuario. For3s ve toda
        la conversación previa como contexto → "recuerda".
        """
        # Aplana el historial a un único prompt legible (en H5/R3 esto pasará
        # a usar el formato messages[] nativo con truncado inteligente).
        lines: list[str] = []
        for turn in history:
            who = "Usuario" if turn["role"] == "user" else "For3s"
            lines.append(f"{who}: {turn['content']}")
        transcript = "\n\n".join(lines)

        if self._oauth:
            prompt = (
                f"[{FOR3S_ROLE}]\n\n"
                "Esta es la conversación hasta ahora (úsala como memoria):\n\n"
                f"{transcript}\n\n"
                "Responde al último mensaje del Usuario."
            )
            return self._provider.complete(prompt, system="", max_tokens=max_tokens)

        prompt = (
            f"Conversación hasta ahora:\n\n{transcript}\n\nResponde al último mensaje del Usuario."
        )
        return self._provider.complete(prompt, system=FOR3S_ROLE, max_tokens=max_tokens)
