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

from for3s_core.llm import ClaudeProvider
from for3s_core.mcp_client import GitHubMCPClient

# Tope de vueltas tool→result→tool por turno. CADA vuelta = 1 request con tools
# (payload pesado). La suscripción topa el rate-limit instantáneo si se encadenan
# muchos requests pesados seguidos (verificado en pruebas Paso 3). 3 vueltas
# cubren el caso normal (ej: list_issues → issue_read → respuesta) sin abusar.
MAX_TOOL_ROUNDS = 3

# Whitelist MVP: las tools de LECTURA esenciales. CLAVE (verificado en pruebas):
# mandar muchos schemas de tool en cada request infla el input y la suscripción
# devuelve 429 (rate_limit_error) por encima de ~4-5 tools con schemas grandes.
# Por eso el MVP usa un set MÍNIMO que cubre lo esencial: listar y leer issues
# y PRs. get_file_contents/search se agregan después (Paso 5) si el payload lo
# permite, o se rota el set según la intención. Menos tools = más barato y sin 429.
MVP_TOOLS = {
    "list_issues",
    "issue_read",
    "list_pull_requests",
    "pull_request_read",
}


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


async def run_tool_loop(
    provider: ClaudeProvider,
    mcp: GitHubMCPClient,
    messages: list[dict],
    *,
    system: str = "",
    max_tokens: int = 2048,
) -> ToolLoopResult:
    """Corre el loop de tool-use hasta que Claude da una respuesta final.

    messages: historial en formato Anthropic (el último es el user actual).
    Devuelve el texto final + métricas + las tools que se llamaron.
    """
    all_tools = await mcp.tools_for_anthropic()
    tools = [t for t in all_tools if t["name"] in MVP_TOOLS]
    out = ToolLoopResult(text="")

    for _ in range(MAX_TOOL_ROUNDS):
        # complete_with_tools es SÍNCRONO (httpx) → to_thread para no bloquear
        data, headers = await asyncio.to_thread(
            provider.complete_with_tools,
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
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
            out.text = "".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
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
            try:
                result_text = await mcp.call_tool(name, args)
            except Exception as exc:  # tool falló: devolver error legible a Claude
                result_text = f"Error ejecutando {name}: {exc}"
            out.tool_calls.append({"name": name, "args": args})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_text[:20_000],  # cap defensivo
                }
            )
        messages.append({"role": "user", "content": tool_results})

    # Se agotaron las rondas sin respuesta final.
    out.text = (
        out.text
        or "Hice varias consultas a GitHub pero no logré cerrar el análisis. "
        "¿Puedes precisar qué necesitas?"
    )
    return out
