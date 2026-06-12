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

import asyncpg

from for3s_core import audit, memory
from for3s_core.agent import Agent
from for3s_core.llm import LLMResponse


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

    async def send(self, message: str, *, max_tokens: int = 1024) -> LLMResponse:
        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)

        # 1) guardar turno del usuario + audit
        await memory.record_turn(self._pool, self._session_id, role="user", content=message)
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "chars": len(message)},
        )

        # 2) reconstruir historial COMPLETO (incluye el turno recién guardado)
        history = await memory.load_history(self._pool, self._session_id)
        prior = [{"role": t.role, "content": t.content} for t in history]

        # 3) el agente responde con el historial como contexto
        resp = self._agent.ask_with_history(prior, max_tokens=max_tokens)

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
