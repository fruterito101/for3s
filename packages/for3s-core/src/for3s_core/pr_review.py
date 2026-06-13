"""Orquestador de análisis de código (H4) — "pega un URL" → reporte QA.

Detecta el tipo de recurso de GitHub (PR · gist · archivo blob), lo trae
(github_tool, token descifrado del SecretStore con KEK), corre lint objetivo
en sandbox aislado, y arma el prompt de REPORTE QA ESTRUCTURADO. Ese prompt
enriquecido entra al flujo normal de Conversation (memoria + audit).

Mantiene Agent/Conversation INTACTOS: solo transforma el mensaje del usuario.
"""

from __future__ import annotations

import asyncpg

from for3s_core import audit, memory, sandbox
from for3s_core.github_tool import (
    GitHubTool,
    GitHubToolError,
    detect_resource,
    detect_short_ref,
    issue_to_context,
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

# Un issue NO es código: no se lintea ni se da veredicto APROBAR/RECHAZAR.
# Se hace TRIAGE: entender el problema y proponer un plan de acción.
ISSUE_INSTRUCTIONS = """Eres For3s OS en modo QA. Analiza el siguiente ISSUE de GitHub y
entrega un TRIAGE estructurado EXACTAMENTE con este formato (en español, conciso):

📋 RESUMEN
(2-3 líneas: cuál es el problema o petición que reporta el issue)

🔍 TIPO
(uno de: 🐛 bug · ✨ feature · ❓ duda · 📄 docs · 🔧 mantenimiento)

🎯 SEVERIDAD
(uno de: 🔴 alta · 🟡 media · 🟢 baja — y por qué en 1 línea)

🧩 INFO QUE FALTA
(qué datos harían falta para resolverlo: pasos de reproducción, versión, logs.
Si está completo, escribe "nada — está bien documentado")

🛠️ PLAN SUGERIDO
(2-4 pasos concretos para abordarlo)

Sé directo y específico."""


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

    # Aviso a declarar en el reporte cuando el repo se resolvió por contexto
    # (referencia corta "el PR 134" sin URL). Vacío si vino el URL completo.
    aviso_repo = ""

    if tipo == "none":
        # ¿Referencia CORTA ("PR 134", "issue #13") sin URL? Resolverla con el
        # último repo visto en la sesión (Bug F). workspace_id ES el session_id
        # en este setup single-user ("brian").
        ref = detect_short_ref(text)
        if ref is None:
            return None  # ni URL ni referencia corta → chat normal
        ref_tipo, ref_num = ref
        last = await memory.get_last_repo(pool, workspace_id)
        if last is None:
            que = "PR" if ref_tipo == "pr" else "issue"
            return (
                f"__DIRECT__📍 Aún no sé de qué repositorio hablas. Pásame el URL "
                f"completo del {que} una vez (ej. https://github.com/owner/repo/...) "
                f"y después podré entender referencias cortas como “el {que} {ref_num}”."
            )
        owner, repo = last
        tipo = ref_tipo
        datos = (owner, repo, ref_num)
        aviso_repo = (
            f"\n\n📍 NOTA: usé el repositorio {owner}/{repo} (el último que "
            "analizamos). Si te referías a otro, pásame el URL completo. "
            "Decláralo al inicio del reporte."
        )

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
        elif tipo == "issue":
            owner, repo, number = datos
            issue = tool.fetch_issue(owner, repo, number)
            archivos = {}  # un issue no es código → no se lintea
            context = issue_to_context(issue)
            audit_detail = {"tipo": "issue", "owner": owner, "repo": repo, "number": number}
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

    # Recordar el repo visto (Bug F): habilita referencias cortas futuras
    # ("el PR 134") sin URL. Solo para recursos con owner/repo (no gists).
    if tipo in ("pr", "issue", "blob"):
        await memory.set_last_repo(pool, workspace_id, audit_detail["owner"], audit_detail["repo"])

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

    # Un issue se analiza como TRIAGE (no como código): sin lint, otra plantilla.
    if tipo == "issue":
        return f"{ISSUE_INSTRUCTIONS}\n\n{context}{aviso}{aviso_repo}"

    lint = _lint_block(archivos)
    return f"{QA_INSTRUCTIONS}\n\n{context}{lint}{aviso}{aviso_repo}"
