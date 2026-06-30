# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""Cliente MCP de For3s OS — puente con servidores MCP HERMANOS de red (v1.1).

ANTES (v1): el bot lanzaba el GitHub MCP server con `docker run ... stdio`. Pero el
bot ahora vive DENTRO de un contenedor SIN acceso al Docker host (decisión sin-DinD
de Brian) → `docker run` fallaba con FileNotFoundError (BUG-9).

AHORA (v1.1 — HERMANOS DE RED): el GitHub MCP server corre como un SERVICIO HERMANO
en el docker-compose (modo `http`, puerto 8082). El bot se conecta por HTTP a
`http://github-mcp:8082/mcp` (DNS interno de la red for3s_net). Cero acceso al Docker
host → se mantiene el diseño de seguridad. El PAT de GitHub viaja en el header
`Authorization: Bearer <PAT>` de cada conexión (el server HTTP autentica por request,
NO por env var) — compatible con la KEK y con el futuro multi-usuario (PAT por persona).

Expone (interfaz IDÉNTICA a la v1, los call sites no cambian):
  • tools_for_anthropic() → tools en formato Anthropic {name, description, input_schema}
  • call_tool(name, args) → ejecuta la tool vía MCP y devuelve el texto del result.

Diseño: la sesión MCP vive mientras el bot corre (start() → aclose()). asyncio nativo.
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger("for3s.mcp")

# Toolsets de lectura para el MVP (R4: GitHub crítico; aquí solo read).
_MVP_TOOLSETS = "issues,pull_requests,repos"

# URL del hermano GitHub MCP (servicio del compose). Override por env para otros
# despliegues. El path /mcp es el endpoint estándar del modo streamable-http.
GITHUB_MCP_URL = os.environ.get("FOR3S_GITHUB_MCP_URL", "http://github-mcp:8082/mcp")


# P4 — MCP GENÉRICO + CONFIG: For3s puede conectar CUALQUIER servidor MCP por HTTP.
# Un servidor se declara con una config (URL + headers), y MCPClient lo gestiona
# igual para todos. GitHub = "el primer MCP configurado" (ver config_github).
@dataclass
class MCPServerConfig:
    """Declaración de un servidor MCP HERMANO: su URL y los headers (auth)."""

    nombre: str  # id legible (ej. "github")
    url: str  # endpoint http del server hermano (ej. http://github-mcp:8082/mcp)
    headers: dict[str, str] = field(default_factory=dict)


def config_github(pat: str, *, read_only: bool = True) -> MCPServerConfig:
    """Config del MCP de GitHub HERMANO (HTTP). El PAT va en el header Authorization
    (el server http autentica por request). `read_only` lo fija el contenedor hermano
    al arrancar (`http --read-only` en el compose), no el cliente — aquí se conserva
    el parámetro por compat de firma. Para writes se usa otro hermano (ver
    GITHUB_MCP_WRITE_URL en ejecutar_write)."""
    return MCPServerConfig(
        nombre="github",
        url=GITHUB_MCP_URL,
        headers={"Authorization": f"Bearer {pat}"},
    )


class MCPClient:
    """Sesión persistente con CUALQUIER servidor MCP HERMANO (HTTP streamable),
    desde una MCPServerConfig. Genérico: GitHub, etc. usan el mismo cliente.
    start/aclose/tools_for_anthropic/call_tool idénticos para todos."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._cfg = config
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._tools_cache: list[dict[str, Any]] | None = None

    @property
    def nombre(self) -> str:
        return self._cfg.nombre

    async def start(self) -> None:
        """Abre la sesión HTTP con el server MCP hermano. Idempotente. Lanza si el
        hermano no responde (el caller lo captura y degrada a sin-GitHub)."""
        if self._session is not None:
            return
        self._stack = AsyncExitStack()
        # streamablehttp_client devuelve (read, write, get_session_id)
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(self._cfg.url, headers=self._cfg.headers)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def aclose(self) -> None:
        """Cierra la sesión. DEFENSIVO: cerrar desde otra tarea (anyio cancel scope)
        lanza RuntimeError — no explota, suelta referencias."""
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except RuntimeError as exc:
                logger.warning(
                    "aclose del MCP %s desde otra tarea (no crítico): %s", self._cfg.nombre, exc
                )
            except Exception:
                logger.warning(
                    "error cerrando el MCP %s (no crítico)", self._cfg.nombre, exc_info=True
                )
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
                {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
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
    """GitHub MCP server HERMANO (HTTP, read-only). Es un caso del MCPClient genérico
    (P4): GitHub = el primer MCP configurado. Mantiene la firma __init__(pat,
    read_only=) para compat total con los call sites existentes."""

    def __init__(self, pat: str, *, read_only: bool = True) -> None:
        super().__init__(config_github(pat, read_only=read_only))


# URL del hermano GitHub MCP WRITE-capable (servicio aparte del compose, SIN
# --read-only). Solo se usa para writes ya confirmadas por el usuario.
GITHUB_MCP_WRITE_URL = os.environ.get(
    "FOR3S_GITHUB_MCP_WRITE_URL", "http://github-mcp-write:8082/mcp"
)


async def ejecutar_write(pat: str, name: str, args: dict[str, Any]) -> str:
    """Ejecuta UNA write tool vía el hermano GitHub MCP WRITE-capable (HTTP).

    Diseño de seguridad (defense in depth), igual que en v1 pero por red:
      • El cliente de LECTURA del bot apunta al hermano read-only SIEMPRE.
      • La escritura usa OTRO hermano (github-mcp-write, SIN --read-only), separado.
      • La whitelist dura del tool_loop (WRITE_TOOLS) garantiza que `name` es una
        write segura permitida; aquí ya llega validada y confirmada por el usuario.
      • Abre y cierra la sesión en el mismo task → sin 'cancel scope in another task'.

    Devuelve el resultado como texto. Lanza la excepción si la tool falla (el caller
    la captura para avisar al usuario).
    """
    headers = {"Authorization": f"Bearer {pat}"}
    async with AsyncExitStack() as stack:
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(GITHUB_MCP_WRITE_URL, headers=headers)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.call_tool(name, args)
        parts: list[str] = []
        for block in result.content:
            parts.append(getattr(block, "text", str(block)))
        return "\n".join(parts)
