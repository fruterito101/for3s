"""Orquestador de análisis de PR (H4) — convierte "pega un URL" en reporte QA.

Flujo: detecta URL de PR → trae el PR (github_tool, token descifrado del
SecretStore con KEK) → arma el prompt de REPORTE QA ESTRUCTURADO → ese
prompt enriquecido entra al flujo normal de Conversation (memoria + audit).

Mantiene Agent/Conversation INTACTOS: solo transforma el mensaje del usuario
en un mensaje enriquecido con el contexto del PR + las instrucciones de QA.
"""

from __future__ import annotations

import asyncpg

from for3s_core import audit, sandbox
from for3s_core.github_tool import GitHubTool, GitHubToolError, parse_pr_url, pr_to_context
from for3s_core.secret_store import SecretStore

# Plantilla del REPORTE QA ESTRUCTURADO (la "cara" del producto — semilla R7 QA Pack).
QA_INSTRUCTIONS = """Eres For3s OS en modo QA. Analiza el siguiente Pull Request y entrega un
REPORTE estructurado EXACTAMENTE con este formato (en español, conciso):

📋 RESUMEN
(2-3 líneas: qué hace este PR)

🔴 CRÍTICOS
(bugs, fallos de seguridad, lógica rota. Si no hay, escribe "ninguno")

🟡 ADVERTENCIAS
(code smells, casos no cubiertos, riesgos. Si no hay, "ninguna")

🟢 SUGERENCIAS
(mejoras opcionales. Si no hay, "ninguna")

⚖️ VEREDICTO
(uno de: ✅ APROBAR · 🔁 REVISAR (con cambios) · ⛔ RECHAZAR — y por qué en 1 línea)

Sé directo y específico. Cita archivos/líneas cuando puedas."""


async def analizar_pr(pool: asyncpg.Pool, workspace_id: str, text: str) -> str | None:
    """Si el texto trae un URL de PR, devuelve el MENSAJE ENRIQUECIDO para el
    agente (contexto del PR + lint objetivo en sandbox + instrucciones QA).
    Si no hay URL, devuelve None (el mensaje sigue su flujo normal de chat).
    """
    parsed = parse_pr_url(text)
    if parsed is None:
        return None
    owner, repo, number = parsed

    # token de GitHub descifrado al vuelo (KEK) — decrypt minimum
    store = SecretStore(pool)
    gh_token = await store.get_secret(workspace_id, "github_token")

    tool = GitHubTool(token=gh_token)
    try:
        pr = tool.fetch_pr(owner, repo, number)
    except GitHubToolError as exc:
        await audit.append(
            pool,
            actor="for3s",
            action="pr_fetch_failed",
            detail={"owner": owner, "repo": repo, "number": number, "error": str(exc)},
        )
        # error legible: se devuelve como "respuesta directa" envuelta
        return f"__DIRECT__{exc}"

    await audit.append(
        pool,
        actor="for3s",
        action="pr_fetched",
        detail={
            "owner": owner,
            "repo": repo,
            "number": number,
            "files": len(pr.files),
            "changed_files": pr.changed_files,
        },
    )

    # lint OBJETIVO en sandbox Docker aislado (degrada a "" si no hay Docker)
    archivos = {f.filename: f.patch_to_source() for f in pr.files}
    lint_findings = sandbox.lint_archivos(archivos)
    lint_block = ""
    if lint_findings:
        lint_block = (
            "\n\nHALLAZGOS OBJETIVOS DEL LINTER (ruff, ejecutado en sandbox "
            f"aislado sobre el código del PR):\n{lint_findings}\n"
            "Incorpóralos al reporte donde corresponda."
        )

    return f"{QA_INSTRUCTIONS}\n\n{pr_to_context(pr)}{lint_block}"
