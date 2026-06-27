"""Cache de lecturas de GitHub en Valkey (2026-06-18, diseño R4.2.1 §4).

Cuando el agente LEE algo de GitHub (un README, un issue, una búsqueda), el
resultado se guarda en Valkey con un TTL. Si en los próximos segundos/minutos se
vuelve a pedir lo MISMO, se sirve de Valkey sin volver a pegarle a la API de
GitHub → más rápido y menos rate-limit.

REGLAS LOCKED (R4.2.1):
  • TTL por tool (CACHEABLE_TOOLS_TTL): cuánto vive cada lectura.
  • NEVER_CACHE: tools cuyo dato cambia tan rápido que no se cachean.
  • Las WRITE tools NUNCA se cachean (ni pasan por aquí — viven en otro camino).
  • La cache key incluye workspace_id → cuando llegue multi-tenant, no se mezcla.

DEFENSIVO POR DISEÑO: el cache es una OPTIMIZACIÓN, nunca un punto de fallo. Si
Valkey no responde (caído, lento, error), TODA función degrada a "sin cache":
get() devuelve None (→ se lee de GitHub normal) y set() no hace nada. El bot
jamás se cae ni se cuelga por el cache.
"""

from __future__ import annotations

import hashlib
import json
import logging

import redis.asyncio as redis

logger = logging.getLogger("for3s.cache")

# TTL (segundos) por tool de LECTURA. Fiel al diseño R4.2.1 §4.
CACHEABLE_TOOLS_TTL = {
    "get_file_contents": 300,
    "pull_request_read": 60,
    "issue_read": 60,
    "list_commits": 180,
    "search_repositories": 1800,
    "search_code": 900,
    "list_pull_requests": 30,
    "list_issues": 30,
    "search_issues": 60,
    "search_pull_requests": 60,
}

# Tools de lectura que NUNCA se cachean (su dato cambia constantemente).
NEVER_CACHE = {"get_pull_request_status", "get_pull_request_files"}

_PREFIJO = "for3s:gh"  # namespace de las keys en Valkey
_TIMEOUT = 1.5         # seg: si Valkey tarda más, degradamos (no bloqueamos al bot)


class GitHubCache:
    """Cache async de lecturas de GitHub sobre Valkey. Tolerante a fallos."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6379) -> None:
        # socket_timeout corto: si Valkey no responde rápido, preferimos leer de
        # GitHub a colgar el turno. El cliente se crea perezoso (no conecta aún).
        self._r = redis.Redis(
            host=host, port=port, decode_responses=True,
            socket_timeout=_TIMEOUT, socket_connect_timeout=_TIMEOUT,
        )

    @staticmethod
    def cacheable(name: str) -> int | None:
        """Devuelve el TTL si la tool es cacheable, o None si no se debe cachear
        (no está en la lista, o está en NEVER_CACHE, o es una write)."""
        if name in NEVER_CACHE:
            return None
        return CACHEABLE_TOOLS_TTL.get(name)

    @staticmethod
    def _key(workspace_id: str, name: str, args: dict) -> str:
        """Key estable: prefijo + workspace + tool + hash de los args. Incluir
        workspace_id evita que dos clientes compartan cache (multi-tenant)."""
        # args ordenados → misma key sin importar el orden de las claves
        firma = json.dumps(args, sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256(firma.encode("utf-8")).hexdigest()[:16]
        return f"{_PREFIJO}:{workspace_id}:{name}:{h}"

    async def get(self, workspace_id: str, name: str, args: dict) -> str | None:
        """Resultado cacheado de (tool, args) o None. Degrada a None si Valkey
        falla o la tool no es cacheable."""
        if self.cacheable(name) is None:
            return None
        try:
            return await self._r.get(self._key(workspace_id, name, args))
        except Exception as exc:  # noqa: BLE001 — cache caído ≠ bot caído
            logger.warning("cache get falló (sigo sin cache): %s", exc)
            return None

    async def set(self, workspace_id: str, name: str, args: dict, value: str) -> None:
        """Guarda el resultado con el TTL de la tool. No-op si no es cacheable o
        si Valkey falla."""
        ttl = self.cacheable(name)
        if ttl is None:
            return
        try:
            await self._r.set(self._key(workspace_id, name, args), value, ex=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache set falló (no crítico): %s", exc)

    async def aclose(self) -> None:
        try:
            await self._r.aclose()
        except Exception:  # noqa: BLE001
            pass
