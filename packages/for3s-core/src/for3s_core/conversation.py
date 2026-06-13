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

import asyncpg

from for3s_core import audit, memory
from for3s_core.agent import Agent
from for3s_core.llm import LLMResponse

# Cuántos turnos recientes se le pasan a Claude como contexto. NO todo el
# historial (sesiones largas de 96k chars colgaban al bot). El resumen del
# historial viejo es R3/H5.
MAX_HISTORY_TURNS = 12


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
        await memory.record_turn(self._pool, self._session_id, role="user", content=message)
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
