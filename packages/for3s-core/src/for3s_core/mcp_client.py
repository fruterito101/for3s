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

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("for3s.mcp")

# Toolsets de lectura para el MVP (R4: GitHub crítico; aquí solo read).
_MVP_TOOLSETS = "issues,pull_requests,repos"


# P4 — MCP GENÉRICO + CONFIG: For3s puede conectar CUALQUIER servidor MCP, no solo
# GitHub. Un servidor se declara con una config (comando que lo arranca + env), y
# MCPClient lo gestiona igual para todos. GitHub pasa a ser "el primer MCP
# configurado" (ver config_github). Enchufar otro = crear otra MCPServerConfig.
@dataclass
class MCPServerConfig:
    """Declaración de un servidor MCP: cómo arrancarlo y qué nombre tiene."""

    nombre: str                       # id legible (ej. "github")
    command: str                      # binario que lo lanza (ej. "docker", "npx")
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def config_github(pat: str, *, read_only: bool = True) -> MCPServerConfig:
    """Config del MCP de GitHub (el primer/único server por ahora). Misma invocación
    que el cliente GitHub hardcodeado tenía — comportamiento idéntico."""
    args = [
        "run", "-i", "--rm",
        "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server",
        "stdio",
        "--toolsets", _MVP_TOOLSETS,
    ]
    if read_only:
        args.append("--read-only")
    return MCPServerConfig(
        nombre="github", command="docker", args=args,
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": pat})


class MCPClient:
    """Sesión persistente con CUALQUIER servidor MCP (stdio), desde una
    MCPServerConfig. Genérico: GitHub, filesystem, Slack, etc. usan el mismo
    cliente. start/aclose/tools_for_anthropic/call_tool idénticos para todos."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._cfg = config
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._tools_cache: list[dict[str, Any]] | None = None

    @property
    def nombre(self) -> str:
        return self._cfg.nombre

    async def start(self) -> None:
        """Lanza el servidor MCP y abre la sesión. Idempotente."""
        if self._session is not None:
            return
        server = StdioServerParameters(
            command=self._cfg.command, args=self._cfg.args, env=self._cfg.env)
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(server))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def aclose(self) -> None:
        """Cierra la sesión y baja el server. DEFENSIVO: cerrar desde otra tarea
        (anyio cancel scope) lanza RuntimeError — no explota, suelta referencias."""
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except RuntimeError as exc:
                logger.warning("aclose del MCP %s desde otra tarea (no crítico): %s",
                               self._cfg.nombre, exc)
            except Exception:
                logger.warning("error cerrando el MCP %s (no crítico)",
                               self._cfg.nombre, exc_info=True)
            finally:
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
                {"name": t.name, "description": t.description or "",
                 "input_schema": t.inputSchema}
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


class GitHubMCPClient(MCPClient):
    """GitHub MCP server (Docker stdio, read-only). Ahora es un caso del MCPClient
    GENÉRICO (P4): GitHub = el primer MCP configurado. Mantiene la firma
    __init__(pat, read_only=) para compat total con los call sites existentes.
    Toda la lógica (start/aclose/tools/call_tool) vive en MCPClient."""

    def __init__(self, pat: str, *, read_only: bool = True) -> None:
        super().__init__(config_github(pat, read_only=read_only))


async def ejecutar_write(pat: str, name: str, args: dict[str, Any]) -> str:
    """Ejecuta UNA write tool en un contenedor MCP EFÍMERO write-capable
    (2026-06-18, write tools con confirmación).

    Diseño de seguridad (defense in depth):
      • El cliente de LECTURA del bot sigue corriendo read-only SIEMPRE.
      • La escritura usa un contenedor APARTE, write-capable, que se levanta
        SOLO para esta llamada (ya confirmada por el usuario con botón) y se
        cierra al terminar. El MCP write NO queda vivo esperando → menor
        superficie de ataque.
      • Levanta su propia sesión (stdio) y la cierra en el mismo task → no hay
        el problema de 'cancel scope in a different task'.

    Devuelve el resultado como texto. Lanza la excepción si la tool falla (el
    caller la captura para avisar al usuario).
    """
    args_run = [
        "run", "-i", "--rm",
        "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server",
        "stdio",
        "--toolsets", _MVP_TOOLSETS,
        # SIN --read-only: este contenedor sí puede escribir. La whitelist dura
        # del tool_loop (WRITE_TOOLS) es la que garantiza que `name` es una write
        # segura permitida; aquí ya llega validado y confirmado por el usuario.
    ]
    server = StdioServerParameters(
        command="docker",
        args=args_run,
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": pat},
    )
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.call_tool(name, args)
        parts: list[str] = []
        for block in result.content:
            parts.append(getattr(block, "text", str(block)))
        return "\n".join(parts)
