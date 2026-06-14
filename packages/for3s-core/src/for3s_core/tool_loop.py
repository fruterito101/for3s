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

from for3s_core.llm import ClaudeProvider, RateLimitExceeded
from for3s_core.mcp_client import GitHubMCPClient

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
ESPACIADO_ENTRE_VUELTAS = 3.0

# Whitelist MVP: las tools de LECTURA esenciales. Antes solo 4 (issues/PRs) →
# por eso "analizar un repo completo" fallaba: el agente no podía LEER el
# contenido del repo (README, archivos). Ahora 6, incluyendo get_file_contents
# (leer README/archivos) y search_code (buscar en el repo) → puede analizar un
# repo de verdad. El riesgo de rate-limit por # de schemas se mitiga con el
# prompt caching (Parte C). Si reaparece el 429 por tamaño, rotar set por intención.
MVP_TOOLS = {
    "list_issues",
    "issue_read",
    "list_pull_requests",
    "pull_request_read",
    "get_file_contents",  # leer README/archivos → analizar repo completo
    "search_code",        # buscar en el repo
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
            out.tool_calls.append({"name": name, "args": args, "result": result_text})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_text[:20_000],  # cap defensivo
                }
            )
        messages.append({"role": "user", "content": tool_results})

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
