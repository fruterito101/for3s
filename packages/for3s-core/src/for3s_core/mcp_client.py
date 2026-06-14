"""Cliente MCP de For3s OS — puente con el GitHub MCP server (Paso 3 migración).

Gestiona la sesión con el GitHub MCP server oficial (contenedor Docker, stdio,
read-only en el MVP). Expone:
  • tools_for_anthropic() → las tools en formato {name, description, input_schema}
    listo para pasarlas a Claude (Messages API).
  • call_tool(name, args) → ejecuta la tool vía MCP y devuelve el texto del result.

El PAT de GitHub se inyecta al contenedor en runtime (NO en .env, NO en texto
plano) — fiel a R4. read-only en el MVP: solo las 21 tools de lectura.

Diseño: la sesión MCP vive mientras el bot corre (se abre en start(), se cierra
en aclose()). asyncio nativo, convive con python-telegram-bot.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Toolsets de lectura para el MVP (R4: GitHub crítico; aquí solo read).
_MVP_TOOLSETS = "issues,pull_requests,repos"


class GitHubMCPClient:
    """Sesión persistente con el GitHub MCP server (Docker stdio, read-only)."""

    def __init__(self, pat: str, *, read_only: bool = True) -> None:
        self._pat = pat
        self._read_only = read_only
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._tools_cache: list[dict[str, Any]] | None = None

    async def start(self) -> None:
        """Lanza el contenedor MCP y abre la sesión. Idempotente."""
        if self._session is not None:
            return
        args = [
            "run", "-i", "--rm",
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "ghcr.io/github/github-mcp-server",
            "stdio",
            "--toolsets", _MVP_TOOLSETS,
        ]
        if self._read_only:
            args.append("--read-only")
        server = StdioServerParameters(
            command="docker",
            args=args,
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": self._pat},
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(server))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def aclose(self) -> None:
        """Cierra la sesión y baja el contenedor."""
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None
            self._tools_cache = None

    async def tools_for_anthropic(self) -> list[dict[str, Any]]:
        """Tools en formato Anthropic: [{name, description, input_schema}]."""
        if self._session is None:
            raise RuntimeError("MCP no iniciado: llama a start() primero")
        if self._tools_cache is None:
            resp = await self._session.list_tools()
            self._tools_cache = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema,
                }
                for t in resp.tools
            ]
        return self._tools_cache

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Ejecuta una tool MCP y devuelve su resultado como texto plano."""
        if self._session is None:
            raise RuntimeError("MCP no iniciado: llama a start() primero")
        result = await self._session.call_tool(name, args)
        parts: list[str] = []
        for block in result.content:
            parts.append(getattr(block, "text", str(block)))
        return "\n".join(parts)
