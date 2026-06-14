"""Conversación con memoria (H2) — une Agent (H1) + memoria + audit.

Orquesta el ciclo completo de un turno CON memoria persistente:
  1. asegura la sesión          (memory.ensure_session)
  2. guarda el turno del user   (memory.record_turn)
  3. reconstruye el historial   (memory.load_history) → se lo pasa al agente
  4. el agente responde         (Agent.ask_with_history)
  5. guarda la respuesta        (memory.record_turn)
  6. escribe en el audit chain  (audit.append)

Así For3s recuerda entre reinicios y cada turno queda auditado. El Agent
sigue PURO (no sabe de Postgres); la persistencia vive aquí.
"""

from __future__ import annotations

import asyncio
import re

import asyncpg

from for3s_core import audit, memory
from for3s_core.agent import FOR3S_ROLE, Agent
from for3s_core.llm import LLMResponse
from for3s_core.mcp_client import GitHubMCPClient
from for3s_core.tool_loop import run_tool_loop

# Cuántos turnos recientes se le pasan a Claude como contexto. NO todo el
# historial (sesiones largas de 96k chars colgaban al bot). El resumen del
# historial viejo es R3/H5.
MAX_HISTORY_TURNS = 12

# Detector LIGERO de "¿este mensaje huele a GitHub?" → solo entonces se le dan
# las tools a Claude (corre el loop MCP). Ahorra rate-limit (el tool-use manda
# schemas pesados; ver hallazgo Paso 3) y mantiene la charla normal ágil.
# Conservador: keywords claras de repo/PR/issue/código, o un URL de GitHub.
_GH_HINT_RE = re.compile(
    r"\b(github\.com|pull\s*request|\bpr\b|issue|repo(sitorio)?|"
    r"commit|branch|pull/\d+|issues?/\d+|c[oó]digo|archivo)\b",
    re.IGNORECASE,
)


def huele_a_github(text: str) -> bool:
    """True si el mensaje parece referirse a GitHub/código → activar tools."""
    return bool(_GH_HINT_RE.search(text))


class Conversation:
    """Una conversación persistente atada a una sesión."""

    def __init__(
        self, pool: asyncpg.Pool, agent: Agent, session_id: str, channel: str = "cli"
    ) -> None:
        self._pool = pool
        self._agent = agent
        self._session_id = session_id
        self._channel = channel

    async def history(self) -> list[memory.Turn]:
        return await memory.load_history(self._pool, self._session_id)

    async def send(
        self, message: str, *, max_tokens: int = 1024, prompt: str | None = None
    ) -> LLMResponse:
        """Procesa un turno.

        message: lo que se GUARDA en memoria (el texto original del usuario, corto).
        prompt:  lo que se MANDA a Claude (puede ser enriquecido, ej. PR completo).
                 Si es None, se manda el mismo `message`.
        Separarlos evita guardar prompts gigantes (contexto de PR de 100k chars)
        en la memoria, que luego inflaban el historial y colgaban al bot.
        """
        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)

        # 1) guardar SOLO el mensaje original (corto) + audit
        await memory.record_turn(
            self._pool, self._session_id, role="user", content=message, channel=self._channel
        )
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "chars": len(message)},
        )

        # 2) reconstruir historial — solo los ÚLTIMOS N turnos (no todo).
        history = await memory.load_history(self._pool, self._session_id, last_n=MAX_HISTORY_TURNS)
        prior = [{"role": t.role, "content": t.content} for t in history]
        # el último turno (el del usuario) se reemplaza por el prompt enriquecido
        # SOLO para mandárselo a Claude — en memoria queda el mensaje corto.
        if prompt is not None and prior:
            prior[-1] = {"role": "user", "content": prompt}

        # 3) el agente responde. ask_with_history es SÍNCRONO (httpx bloqueante);
        # to_thread libera el event loop → el bot no se congela y el wait_for
        # del canal SÍ puede cortar (bug del PR #134).
        resp = await asyncio.to_thread(self._agent.ask_with_history, prior, max_tokens=max_tokens)

        # 4) guardar respuesta + audit
        await memory.record_turn(
            self._pool,
            self._session_id,
            role="assistant",
            content=resp.text,
            tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens,
            model=resp.model,
            channel=self._channel,
        )
        await audit.append(
            self._pool,
            actor="for3s",
            action="message_out",
            detail={
                "session": self._session_id,
                "tokens_in": resp.input_tokens,
                "tokens_out": resp.output_tokens,
                "model": resp.model,
            },
        )
        return resp

    async def send_with_tools(
        self, message: str, mcp: GitHubMCPClient, *, max_tokens: int = 2048
    ) -> LLMResponse:
        """Turno CON tools de GitHub (migración MCP, Paso 4-6).

        Como send() pero deja que el MODELO decida usar las tools GitHub (vía
        MCP), en vez del regex artesanal. Reusa memoria + audit. El system es
        FOR3S_ROLE; en modo OAuth va antepuesto al mensaje (la suscripción
        rechaza system custom). Persiste lo que las tools traen (gh_resources).
        """
        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)

        # 1) guardar el mensaje del usuario + audit
        await memory.record_turn(
            self._pool, self._session_id, role="user", content=message, channel=self._channel
        )
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "chars": len(message)},
        )

        # 2) historial reciente → messages[] formato Anthropic
        history = await memory.load_history(self._pool, self._session_id, last_n=MAX_HISTORY_TURNS)
        messages = [{"role": t.role, "content": t.content} for t in history]

        # OAuth: la identidad For3s va en el system de Claude Code + el rol en
        # el último mensaje (la suscripción no admite system custom). En API key
        # el rol va como system del loop.
        provider = self._agent._provider
        oauth = getattr(self._agent, "_oauth", False)
        if oauth:
            system = ""
            if messages and messages[-1]["role"] == "user":
                messages[-1] = {"role": "user", "content": f"[{FOR3S_ROLE}]\n\n{message}"}
            else:
                messages.append({"role": "user", "content": f"[{FOR3S_ROLE}]\n\n{message}"})
        else:
            system = FOR3S_ROLE

        # 3) correr el loop de tool-use (el modelo decide usar GitHub o no)
        result = await run_tool_loop(
            provider, mcp, messages, system=system, max_tokens=max_tokens
        )

        # 4) persistir lo que las tools trajeron de GitHub (Paso 4)
        if result.tool_calls:
            await memory.save_gh_tool_calls(
                self._pool,
                session_id=self._session_id,
                tool_calls=result.tool_calls,
            )

        # 5) guardar respuesta + audit (el reporte, no el contexto crudo)
        await memory.record_turn(
            self._pool,
            self._session_id,
            role="assistant",
            content=result.text,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            model=result.model,
            channel=self._channel,
        )
        await audit.append(
            self._pool,
            actor="for3s",
            action="message_out",
            detail={
                "session": self._session_id,
                "tokens_in": result.input_tokens,
                "tokens_out": result.output_tokens,
                "model": result.model,
                "tools": [tc["name"] for tc in result.tool_calls],
            },
        )

        return LLMResponse(
            text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model=result.model,
            usage_5h=result.usage_5h,
            usage_7d=result.usage_7d,
        )
