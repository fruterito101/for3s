"""Orquestador de análisis de código (H4) — "pega un URL" → reporte QA.

Detecta el tipo de recurso de GitHub (PR · gist · archivo blob), lo trae
(github_tool, token descifrado del SecretStore con KEK), corre lint objetivo
en sandbox aislado, y arma el prompt de REPORTE QA ESTRUCTURADO. Ese prompt
enriquecido entra al flujo normal de Conversation (memoria + audit).

Mantiene Agent/Conversation INTACTOS: solo transforma el mensaje del usuario.
"""

from __future__ import annotations

import asyncpg

from for3s_core import audit, sandbox
from for3s_core.github_tool import (
    GitHubTool,
    GitHubToolError,
    detect_resource,
    pr_to_context,
    snippet_to_context,
)
from for3s_core.secret_store import SecretStore

# Cap del contexto a Claude: analizable en <1 min (un PR de 63k chars colgaba
# el bot). El análisis por lotes/multi-agente completo llega en H8 (R5).
MAX_CONTEXT_CHARS = 25_000

# Plantilla del REPORTE QA ESTRUCTURADO (la "cara" del producto — semilla R7 QA Pack).
QA_INSTRUCTIONS = """Eres For3s OS en modo QA. Analiza el siguiente código y entrega un
REPORTE estructurado EXACTAMENTE con este formato (en español, conciso):

📋 RESUMEN
(2-3 líneas: qué hace este código)

🔴 CRÍTICOS
(bugs, fallos de seguridad, lógica rota. Si no hay, escribe "ninguno")

🟡 ADVERTENCIAS
(code smells, casos no cubiertos, riesgos. Si no hay, "ninguna")

🟢 SUGERENCIAS
(mejoras opcionales. Si no hay, "ninguna")

⚖️ VEREDICTO
(uno de: ✅ APROBAR · 🔁 REVISAR (con cambios) · ⛔ RECHAZAR — y por qué en 1 línea)

Sé directo y específico. Cita archivos/líneas cuando puedas."""


def _lint_block(archivos: dict[str, str]) -> str:
    """Corre el lint objetivo en sandbox y arma el bloque para el prompt."""
    findings = sandbox.lint_archivos(archivos)
    if not findings:
        return ""
    return (
        "\n\nHALLAZGOS OBJETIVOS DEL LINTER (ruff, ejecutado en sandbox "
        f"aislado):\n{findings}\nIncorpóralos al reporte donde corresponda."
    )


async def analizar_pr(pool: asyncpg.Pool, workspace_id: str, text: str) -> str | None:
    """Si el texto trae un recurso de GitHub (PR/gist/archivo), devuelve el
    MENSAJE ENRIQUECIDO para el agente (contexto + lint + instrucciones QA).
    Si no hay recurso, devuelve None (sigue su flujo normal de chat).
    """
    tipo, datos = detect_resource(text)
    if tipo == "none":
        return None

    store = SecretStore(pool)
    gh_token = await store.get_secret(workspace_id, "github_token")
    tool = GitHubTool(token=gh_token)

    try:
        if tipo == "pr":
            owner, repo, number = datos
            pr = tool.fetch_pr(owner, repo, number)
            archivos = {f.filename: f.patch_to_source() for f in pr.files}
            context = pr_to_context(pr)
            audit_detail = {"tipo": "pr", "owner": owner, "repo": repo, "number": number}
        elif tipo == "gist":
            (gist_id,) = datos
            snip = tool.fetch_gist(gist_id)
            archivos = snip.files
            context = snippet_to_context(snip)
            audit_detail = {"tipo": "gist", "gist_id": gist_id}
        else:  # blob
            owner, repo, ref, path = datos
            snip = tool.fetch_file(owner, repo, ref, path)
            archivos = snip.files
            context = snippet_to_context(snip)
            audit_detail = {"tipo": "blob", "owner": owner, "repo": repo, "path": path}
    except GitHubToolError as exc:
        await audit.append(
            pool, actor="for3s", action="gh_fetch_failed", detail={"error": str(exc)}
        )
        return f"__DIRECT__{exc}"

    await audit.append(pool, actor="for3s", action="gh_fetched", detail=audit_detail)

    # CAP de contexto: un PR enorme (ej. 63k chars) hace que Claude tarde
    # minutos y el bot se atasque (bug del PR #134). Acotamos a un tamaño
    # analizable en <1 min y avisamos lo que se recortó. El análisis por
    # lotes/multi-agente completo es H8 (R5); esto es la versión simple.
    aviso = ""
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS]
        aviso = (
            "\n\n⚠️ NOTA: este código es muy grande; analicé solo la primera "
            "parte. Para un análisis completo, pásame archivos/secciones "
            "específicas. Decláralo en el reporte."
        )

    lint = _lint_block(archivos)
    return f"{QA_INSTRUCTIONS}\n\n{context}{lint}{aviso}"
