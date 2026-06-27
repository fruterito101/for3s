"""Loop de tool-use de For3s OS (Paso 3 migración GitHub→MCP).

El corazón del agente "con manos" estándar: deja que el MODELO decida qué tool
de GitHub usar (vía MCP), en vez del regex artesanal (detect_resource).

Flujo (patrón estándar de Anthropic tool-use):
  1. messages = [user]
  2. Claude responde. ¿stop_reason == "tool_use"?
     • SÍ → ejecutar cada tool vía MCP, añadir tool_result, repetir
     • NO → devolver el texto final
  3. tope de iteraciones (MAX_TOOL_ROUNDS) por seguridad.

NO toca el flujo viejo (complete/ask_with_history). Es la pieza nueva que se
probará aislada antes de conectarla al bot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from for3s_core.llm import ClaudeProvider, RateLimitExceeded
from for3s_core.mcp_client import GitHubMCPClient

if TYPE_CHECKING:
    from for3s_core.cache import GitHubCache

# Tope de vueltas tool→result→tool por turno. CADA vuelta = 1 request con tools
# (payload pesado). La suscripción topa el rate-limit instantáneo si se encadenan
# muchos requests pesados seguidos (verificado en pruebas Paso 3). 5 vueltas:
# 3 se quedaban cortas para conteos que paginan (Fallo 2: "cuántos PR cerrados"
# → list_pull_requests ×4 agotaba el loop). 5 cubre paginación moderada sin
# disparar el rate-limit en uso normal.
MAX_TOOL_ROUNDS = 5

# Parte A (anti-rate-limit): segundos a esperar ENTRE vueltas del loop. La
# suscripción NO expone el rate-limit por-minuto en headers, así que vamos a
# ciegas → mejor espaciar las llamadas (cada vuelta reenvía schemas pesados).
# Solo aplica entre vueltas (no antes de la primera) → no ralentiza el caso
# de 1 sola llamada.
# Anexo R3: el control real es por USO (fila de 1, await secuencial). Esto queda
# solo como red de seguridad minima entre vueltas (ya no es el mecanismo principal).
ESPACIADO_ENTRE_VUELTAS = 0.5

# Whitelist MVP: las tools de LECTURA esenciales. Antes solo 4 (issues/PRs) →
# por eso "analizar un repo completo" fallaba: el agente no podía LEER el
# contenido del repo (README, archivos). Ahora 8, incluyendo get_file_contents
# (leer README/archivos), search_code (buscar en el repo) y search_issues/
# search_pull_requests (CONTAR exacto vía total_count en 1 llamada — el dueño
# 2026-06-18: antes los conteos grandes quedaban parciales porque se paginaba
# con list_* y se agotaba MAX_TOOL_ROUNDS; search_* da el número exacto sin
# paginar). El riesgo de rate-limit por # de schemas (+31%) se mitiga con el
# prompt caching (Parte C). Si reaparece el 429 por tamaño, rotar set por intención.
MVP_TOOLS = {
    "list_issues",
    "issue_read",
    "list_pull_requests",
    "pull_request_read",
    "get_file_contents",  # leer README/archivos → analizar repo completo
    "search_code",  # buscar en el repo
    "search_issues",  # CONTAR issues exacto (total_count) en 1 llamada
    "search_pull_requests",  # CONTAR PRs exacto (total_count) en 1 llamada
}

# ─────────────────────── WRITE TOOLS (con confirmación) ───────────────────────
# 2026-06-18: For3s pasa de read-only a poder ESCRIBIR en GitHub, pero SOLO
# un subconjunto SEGURO y REVERSIBLE, y SIEMPRE con confirmación humana (botón en
# Telegram). NADA destructivo (sin merge, sin delete/create repo, sin push de
# archivos). Diseño R4.2.1: estas 4 son clase "write" (mutaciones acumulativas).
#
# WHITELIST DURA: aunque el modelo PIDA otra write (delete_repository, merge…),
# el gate la rechaza porque NO está aquí. Es la garantía de seguridad central.
#
# El cliente MCP de lectura corre read-only → NO expone estas tools. Por eso
# INYECTAMOS sus schemas a mano (controlamos exactamente los campos). El agente
# las PROPONE; el loop NO las ejecuta (gate de intención) → se ejecutan solo
# tras el clic de confirmación, en un contenedor write-capable efímero.
WRITE_TOOLS_PERMITIDAS = {
    "add_issue_comment",
    "create_issue",
    "create_pull_request",
    "create_pull_request_review",
}

# Schemas mínimos de las write tools (campos esenciales del GitHub MCP server).
# Se inyectan junto a las read para que Claude pueda PROPONERLAS.
WRITE_TOOL_SCHEMAS = [
    {
        "name": "add_issue_comment",
        "description": "Comenta en un issue o pull request existente. REQUIERE "
        "confirmación del usuario antes de ejecutarse.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Dueño del repo"},
                "repo": {"type": "string", "description": "Nombre del repo"},
                "issue_number": {"type": "integer", "description": "Número del issue/PR"},
                "body": {"type": "string", "description": "Texto del comentario"},
            },
            "required": ["owner", "repo", "issue_number", "body"],
        },
    },
    {
        "name": "create_issue",
        "description": "Crea un issue nuevo en un repo. REQUIERE confirmación del "
        "usuario antes de ejecutarse.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string", "description": "Título del issue"},
                "body": {"type": "string", "description": "Cuerpo del issue"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Crea un pull request. REQUIERE confirmación del usuario "
        "antes de ejecutarse. NO mergea (eso es destructivo y no "
        "está permitido).",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "head": {"type": "string", "description": "Rama con los cambios"},
                "base": {"type": "string", "description": "Rama destino"},
                "body": {"type": "string"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "create_pull_request_review",
        "description": "Crea un review/comentario en un PR. REQUIERE confirmación "
        "del usuario. Usa event=COMMENT (no APPROVE/REQUEST_CHANGES "
        "sin pedirlo explícito).",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pull_number": {"type": "integer"},
                "body": {"type": "string"},
                "event": {"type": "string", "description": "COMMENT, APPROVE o REQUEST_CHANGES"},
            },
            "required": ["owner", "repo", "pull_number", "body", "event"],
        },
    },
]


def _pct(headers: dict, name: str) -> float | None:
    """Lee un header de utilización de cupo (0..1) como float, o None."""
    v = headers.get(name)
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


@dataclass
class ToolLoopResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    usage_5h: float | None = None
    usage_7d: float | None = None
    tool_calls: list[dict] = field(default_factory=list)  # auditoría: qué tools se usaron
    # Si el agente PROPUSO una write tool (comentar/crear), aquí queda {name, args}
    # pendiente de confirmación humana. El canal de Telegram la convierte en un
    # botón. None = no hay nada que confirmar (turno read-only normal).
    accion_pendiente: dict | None = None


async def run_tool_loop(
    provider: ClaudeProvider,
    mcp: GitHubMCPClient,
    messages: list[dict],
    *,
    system: str = "",
    max_tokens: int = 2048,
    cache: GitHubCache | None = None,
    workspace_id: str = "default",
) -> ToolLoopResult:
    """Corre el loop de tool-use hasta que Claude da una respuesta final.

    messages: historial en formato Anthropic (el último es el user actual).
    Devuelve el texto final + métricas + las tools que se llamaron.

    cache: cache Valkey opcional (2026-06-18). Si se pasa, las lecturas
    cacheables de GitHub se sirven de Valkey cuando hay hit → menos llamadas a
    la API y menos rate-limit. Si es None, funciona igual sin cache (degrada).
    workspace_id: namespace de la cache key (multi-tenant futuro).
    """
    all_tools = await mcp.tools_for_anthropic()
    tools = [t for t in all_tools if t["name"] in MVP_TOOLS]
    # Inyectar las write tools seguras (el MCP read-only no las expone). El agente
    # puede PROPONERLAS; el gate de abajo NO las ejecuta → van a confirmación.
    tools = tools + WRITE_TOOL_SCHEMAS
    out = ToolLoopResult(text="")

    for vuelta in range(MAX_TOOL_ROUNDS):
        # Parte A: espaciar las vueltas (no antes de la primera) para no saturar
        # el rate-limit por-minuto con llamadas tool-use seguidas.
        if vuelta > 0:
            await asyncio.sleep(ESPACIADO_ENTRE_VUELTAS)
        # H-F: en la PRIMERA vuelta forzar el uso de tool (tool_choice="any")
        # → el modelo NO puede narrar/inventar, tiene que ejecutar. En las
        # vueltas siguientes vuelve a "auto" para que pueda RESPONDER con el
        # resultado (si forzáramos siempre, nunca daría la respuesta final).
        tool_choice = {"type": "any"} if (vuelta == 0 and tools) else None
        # complete_with_tools es SÍNCRONO (httpx) → to_thread para no bloquear
        data, headers = await asyncio.to_thread(
            provider.complete_with_tools,
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )

        usage = data.get("usage", {})
        out.input_tokens += usage.get("input_tokens", 0)
        out.output_tokens += usage.get("output_tokens", 0)
        out.model = data.get("model", provider._model)

        out.usage_5h = _pct(headers, "anthropic-ratelimit-unified-5h-utilization") or out.usage_5h
        out.usage_7d = _pct(headers, "anthropic-ratelimit-unified-7d-utilization") or out.usage_7d

        content = data.get("content", [])
        stop = data.get("stop_reason")

        if stop != "tool_use":
            # respuesta final: juntar el texto
            out.text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            return out

        # Claude pidió 1+ tools. Añadir su turno (assistant) tal cual al historial.
        messages.append({"role": "assistant", "content": content})

        # Ejecutar cada tool_use vía MCP y armar los tool_result.
        tool_results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            args = block.get("input", {})
            tool_id = block.get("id")

            # ── GATE DE SEGURIDAD ────────────────────────────────────────────
            # 1) WRITE permitida → NO ejecutar. Capturar como acción pendiente de
            #    confirmación humana y devolver al modelo un tool_result que lo
            #    haga CERRAR el turno (el canal mostrará el botón). Solo la PRIMERA
            #    write propuesta se toma (una confirmación por turno, simple/seguro).
            if name in WRITE_TOOLS_PERMITIDAS:
                if out.accion_pendiente is None:
                    out.accion_pendiente = {"name": name, "args": args}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": (
                            "ACCIÓN PROPUESTA, NO EJECUTADA. Esta acción de escritura "
                            "requiere que el usuario la confirme con un botón. NO la "
                            "repitas ni intentes otra herramienta: responde en UNA "
                            "frase qué vas a hacer y dile al usuario que confirme abajo."
                        ),
                    }
                )
                continue
            # 2) Cualquier write/destructive NO permitida (delete_repository,
            #    merge_pull_request, push_files…) → RECHAZO DURO. Nunca se ejecuta.
            if name not in MVP_TOOLS:
                result_text = (
                    f"BLOQUEADO: la herramienta '{name}' no está permitida. For3s "
                    "solo puede comentar y crear issues/PRs (con confirmación), y "
                    "leer. NO puede mergear, borrar, ni modificar archivos. Dile "
                    "esto al usuario con honestidad."
                )
                out.tool_calls.append({"name": name, "args": args, "result": result_text})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_text,
                    }
                )
                continue
            # 3) READ permitida → cache primero, si no, ejecutar.
            # CACHE (Valkey): si la tool es cacheable y hay hit, servimos de
            # Valkey sin pegarle a GitHub (menos llamadas, menos rate-limit). El
            # cache degrada solo si Valkey falla (get devuelve None → se lee).
            result_text = None
            cacheado = False
            if cache is not None:
                hit = await cache.get(workspace_id, name, args)
                if hit is not None:
                    result_text = hit
                    cacheado = True
            if result_text is None:
                try:
                    result_text = await mcp.call_tool(name, args)
                    # guardar en cache SOLO si la tool es cacheable (cache.set
                    # ya filtra never-cache/write y degrada si Valkey falla).
                    if cache is not None:
                        await cache.set(workspace_id, name, args, result_text)
                except Exception as exc:  # tool falló: error legible a Claude
                    result_text = f"Error ejecutando {name}: {exc}"
            out.tool_calls.append(
                {
                    "name": name,
                    "args": args,
                    "result": result_text,
                    "cacheado": cacheado,
                }
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_text[:20_000],  # cap defensivo
                }
            )
        messages.append({"role": "user", "content": tool_results})

        # Si se propuso una write, no seguimos iterando: dejamos que el modelo dé
        # su frase final (en la siguiente vuelta verá el tool_result y cerrará).
        # El canal toma out.accion_pendiente y muestra el botón de confirmación.

    # Se agotaron las rondas. En vez de un fallback pobre, una ÚLTIMA llamada
    # SIN tools pidiendo a Claude que responda con lo que YA recabó (Fallo 2:
    # conteos que paginan agotaban el loop y daban "no logré cerrar"). Así da
    # una respuesta útil/parcial con los datos que sí trajo.
    messages.append(
        {
            "role": "user",
            "content": (
                "Ya no consultes más herramientas. Responde AHORA con la mejor "
                "respuesta posible usando los datos que ya obtuviste arriba. Si "
                "el conteo quedó incompleto por tamaño, dilo y da el número "
                "aproximado o lo que alcanzaste a ver."
            ),
        }
    )
    try:
        data, headers = await asyncio.to_thread(
            provider.complete_with_tools, messages, system=system, tools=None, max_tokens=max_tokens
        )
        out.text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        usage = data.get("usage", {})
        out.input_tokens += usage.get("input_tokens", 0)
        out.output_tokens += usage.get("output_tokens", 0)
    except RateLimitExceeded:
        raise  # el rate-limit DEBE propagarse para que el canal avise al usuario
    except Exception:
        pass
    out.text = (
        out.text
        or "Consulté GitHub pero el resultado fue muy grande para cerrarlo de una. "
        "¿Puedes acotar (ej. solo abiertos, o un rango)?"
    )
    return out
