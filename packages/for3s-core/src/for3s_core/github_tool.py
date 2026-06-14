"""[DEPRECADO 2026-06-14] GitHub tool ARTESANAL de For3s OS (H4).

DEPRECADO: el bot ya NO usa este modulo. La integracion GitHub migro al
GitHub MCP server oficial (mcp_client.py + tool_loop.py + send_with_tools),
donde el MODELO decide las tools en vez del regex de aqui. Se conserva como
red de seguridad durante el pulido del MVP (comparar/revertir). Se borrara
cuando el MVP este declarado pulido. Ver: Cuerpo/Ronda_04_Anexo_GitHub_*.md.

--- doc original ---
GitHub tool de For3s OS (H4) — la mano que va a leer Pull Requests.

Dado un URL de PR, trae por la API de GitHub: título, descripción, metadata,
archivos cambiados y diff. Manejo de errores NIVEL PRODUCTO: PR inexistente,
repo privado sin permiso, rate limit de GitHub, diff gigante (truncado
inteligente con aviso de lo omitido).

Solo LECTURA (GET). Escribir en PRs (comentar/aprobar) llega en H13.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from for3s_core.text_normalize import normalizar

API = "https://api.github.com"
PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)/pull/(?P<number>\d+)"
)
# Issue: github.com/owner/repo/issues/<n>  (ruta distinta a /pull/)
ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)/issues/(?P<number>\d+)"
)
# Gist: gist.github.com/usuario/<id>  o  gist.github.com/<id>
GIST_URL_RE = re.compile(r"https?://gist\.github\.com/(?:[\w.\-]+/)?(?P<gist_id>[0-9a-f]+)")

# Referencia CORTA (sin URL): "PR 134", "pull request 134", "PR #134",
# "issue 13", "issue #13". CONSERVADOR a propósito: exige la palabra clave
# (pr/pull request/issue) PEGADA al número, para no confundir un número
# cualquiera ("dame 3 ideas", "tengo 134 pesos") con un recurso de GitHub.
# El repo (owner/repo) NO viene aquí — se resuelve con el "último repo visto".
SHORT_PR_RE = re.compile(r"\b(?:pull\s*request|pr)\s*#?\s*(?P<number>\d+)\b", re.IGNORECASE)
SHORT_ISSUE_RE = re.compile(r"\bissue\s*#?\s*(?P<number>\d+)\b", re.IGNORECASE)
# Archivo suelto: github.com/owner/repo/blob/<ref>/<path>
BLOB_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)/blob/(?P<ref>[\w.\-/]+?)/(?P<path>[\w./\-]+)"
)

# Límites de truncado (producto: no reventar el contexto de Claude)
MAX_FILES = 30
MAX_PATCH_CHARS_PER_FILE = 6_000
MAX_TOTAL_PATCH_CHARS = 60_000
MAX_FILE_CHARS = 40_000  # para gists/archivos sueltos


class GitHubToolError(Exception):
    """Error legible para el usuario (se muestra tal cual en el chat)."""


@dataclass
class PRFile:
    filename: str
    status: str  # added | modified | removed | renamed
    additions: int
    deletions: int
    patch: str  # diff del archivo (posiblemente truncado)
    truncated: bool = False

    def patch_to_source(self) -> str:
        """Aproxima el código NUEVO desde el diff: toma las líneas añadidas (+).

        No es el archivo completo (el diff solo trae el contexto del cambio),
        pero sirve para que el linter detecte problemas en lo que se agregó.
        Las líneas de contexto (sin +/-) también se incluyen para dar sintaxis.
        """
        lines = []
        for raw in self.patch.splitlines():
            if raw.startswith("@@") or raw.startswith("---") or raw.startswith("+++"):
                continue
            if raw.startswith("+"):
                lines.append(raw[1:])
            elif raw.startswith("-"):
                continue  # línea eliminada: no va al código nuevo
            else:
                lines.append(raw[1:] if raw.startswith(" ") else raw)
        return "\n".join(lines)


@dataclass
class PullRequest:
    owner: str
    repo: str
    number: int
    title: str
    body: str
    author: str
    state: str
    base: str
    head: str
    additions: int
    deletions: int
    changed_files: int
    files: list[PRFile] = field(default_factory=list)
    omitted_files: int = 0  # archivos no incluidos por truncado

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


def parse_pr_url(text: str) -> tuple[str, str, int] | None:
    """Detecta un URL de PR de GitHub en un texto libre (invocación natural)."""
    m = PR_URL_RE.search(text)
    if not m:
        return None
    return m.group("owner"), m.group("repo"), int(m.group("number"))


@dataclass
class CodeSnippet:
    """Código suelto traído de un gist o archivo (no es un PR)."""

    source: str  # descripción legible de dónde vino
    files: dict[str, str]  # {nombre: contenido}


@dataclass
class IssueComment:
    author: str
    body: str


@dataclass
class Issue:
    """Un issue de GitHub: título, descripción y comentarios (no tiene diff)."""

    owner: str
    repo: str
    number: int
    title: str
    body: str
    author: str
    state: str  # open | closed
    labels: list[str] = field(default_factory=list)
    comments: list[IssueComment] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"


def detect_resource(text: str) -> tuple[str, tuple]:
    """Detecta qué recurso de GitHub trae el texto. (tipo, datos).

    tipo ∈ {"pr", "issue", "gist", "blob", "none"}. PR/issue/blob antes que gist.
    """
    pr = PR_URL_RE.search(text)
    if pr:
        return "pr", (pr.group("owner"), pr.group("repo"), int(pr.group("number")))
    issue = ISSUE_URL_RE.search(text)
    if issue:
        return "issue", (
            issue.group("owner"),
            issue.group("repo"),
            int(issue.group("number")),
        )
    blob = BLOB_URL_RE.search(text)
    if blob:
        return "blob", (
            blob.group("owner"),
            blob.group("repo"),
            blob.group("ref"),
            blob.group("path"),
        )
    gist = GIST_URL_RE.search(text)
    if gist:
        return "gist", (gist.group("gist_id"),)
    return "none", ()


def detect_short_ref(text: str) -> tuple[str, int] | None:
    """Detecta una referencia CORTA a un PR/issue (sin URL): ("pr"|"issue", n).

    Solo activa con palabra clave + número ("PR 134", "issue #13") — NO con un
    número suelto. Devuelve None si no hay referencia clara. El repo se resuelve
    aparte (último repo visto en la sesión). PR tiene prioridad sobre issue.

    Normaliza primero (minúsculas + sin acentos) → robusto ante "PR"/"pr"/"Pr".
    NOTA: detect_resource (URLs completos) NO se normaliza — las URLs son
    case-sensitive (gist_id hex, paths).
    """
    text = normalizar(text)
    pr = SHORT_PR_RE.search(text)
    if pr:
        return "pr", int(pr.group("number"))
    issue = SHORT_ISSUE_RE.search(text)
    if issue:
        return "issue", int(issue.group("number"))
    return None


class GitHubTool:
    """Cliente de solo-lectura de PRs/gists/archivos. Token descifrado (KEK)."""

    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "for3s-os",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        # errores de red/timeout → mensaje legible (no traceback)
        try:
            resp = httpx.get(
                f"{API}{path}", headers=self._headers(), params=params, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise GitHubToolError(
                "No pude conectarme a GitHub ahora mismo. Revisa tu conexión o intenta de nuevo."
            ) from exc
        if resp.status_code == 404:
            raise GitHubToolError(
                "No encontré ese recurso. Verifica el URL, o si es privado "
                "puede que mi token no tenga acceso."
            )
        if resp.status_code in (401, 403):
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise GitHubToolError(
                    "GitHub me puso límite de peticiones por ahora. Intenta en unos minutos."
                )
            raise GitHubToolError(
                "GitHub me negó el acceso (token inválido o sin permisos para ese recurso)."
            )
        if resp.status_code >= 500:
            raise GitHubToolError(
                f"GitHub tuvo un problema temporal (error {resp.status_code}). "
                "Intenta de nuevo en unos segundos."
            )
        if resp.status_code >= 400:
            raise GitHubToolError(f"GitHub respondió con error {resp.status_code}.")
        return resp

    def fetch_pr(self, owner: str, repo: str, number: int) -> PullRequest:
        """Trae el PR completo (metadata + archivos con diff), con truncado."""
        meta = self._get(f"/repos/{owner}/{repo}/pulls/{number}").json()

        pr = PullRequest(
            owner=owner,
            repo=repo,
            number=number,
            title=meta.get("title") or "",
            body=(meta.get("body") or "")[:3000],
            author=(meta.get("user") or {}).get("login", "?"),
            state=meta.get("state", "?"),
            base=(meta.get("base") or {}).get("ref", "?"),
            head=(meta.get("head") or {}).get("ref", "?"),
            additions=meta.get("additions", 0),
            deletions=meta.get("deletions", 0),
            changed_files=meta.get("changed_files", 0),
        )

        # archivos (paginado; cap MAX_FILES con aviso)
        files_raw: list[dict] = []
        page = 1
        while len(files_raw) < MAX_FILES:
            batch = self._get(
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            ).json()
            if not batch:
                break
            files_raw.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        pr.omitted_files = max(0, pr.changed_files - min(len(files_raw), MAX_FILES))

        total_patch = 0
        for fr in files_raw[:MAX_FILES]:
            patch = fr.get("patch") or "(binario o demasiado grande — GitHub no da diff)"
            truncated = False
            if len(patch) > MAX_PATCH_CHARS_PER_FILE:
                patch = patch[:MAX_PATCH_CHARS_PER_FILE] + "\n... [diff truncado]"
                truncated = True
            if total_patch + len(patch) > MAX_TOTAL_PATCH_CHARS:
                pr.omitted_files += len(files_raw[:MAX_FILES]) - len(pr.files)
                break
            total_patch += len(patch)
            pr.files.append(
                PRFile(
                    filename=fr.get("filename", "?"),
                    status=fr.get("status", "?"),
                    additions=fr.get("additions", 0),
                    deletions=fr.get("deletions", 0),
                    patch=patch,
                    truncated=truncated,
                )
            )
        return pr

    def fetch_issue(self, owner: str, repo: str, number: int) -> Issue:
        """Trae un issue: título, descripción, labels y comentarios (cap 20)."""
        meta = self._get(f"/repos/{owner}/{repo}/issues/{number}").json()
        # /issues/N también responde para PRs; si es un PR, redirigir mejor.
        if meta.get("pull_request"):
            raise GitHubToolError(
                "Ese número es un Pull Request, no un issue. Pásame el URL con /pull/."
            )
        labels = [
            (lb.get("name") if isinstance(lb, dict) else str(lb))
            for lb in (meta.get("labels") or [])
        ]
        issue = Issue(
            owner=owner,
            repo=repo,
            number=number,
            title=meta.get("title") or "",
            body=(meta.get("body") or "")[:6000],
            author=(meta.get("user") or {}).get("login", "?"),
            state=meta.get("state", "?"),
            labels=[lb for lb in labels if lb],
        )
        if meta.get("comments"):
            raw = self._get(
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                params={"per_page": 20},
            ).json()
            for c in raw[:20]:
                issue.comments.append(
                    IssueComment(
                        author=(c.get("user") or {}).get("login", "?"),
                        body=(c.get("body") or "")[:2000],
                    )
                )
        return issue

    def fetch_gist(self, gist_id: str) -> CodeSnippet:
        """Trae un gist (puede tener varios archivos)."""
        data = self._get(f"/gists/{gist_id}").json()
        files = {}
        for name, info in (data.get("files") or {}).items():
            files[name] = (info.get("content") or "")[:MAX_FILE_CHARS]
        desc = data.get("description") or "(sin descripción)"
        owner = (data.get("owner") or {}).get("login", "?")
        return CodeSnippet(source=f"Gist de {owner}: {desc}", files=files)

    def fetch_file(self, owner: str, repo: str, ref: str, path: str) -> CodeSnippet:
        """Trae un archivo suelto de un repo (github.com/.../blob/ref/path)."""
        import base64

        data = self._get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref}).json()
        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        except Exception:
            content = "(no se pudo decodificar — ¿binario?)"
        return CodeSnippet(
            source=f"Archivo {owner}/{repo}/{path} @ {ref}",
            files={path.split("/")[-1]: content[:MAX_FILE_CHARS]},
        )


def snippet_to_context(snip: CodeSnippet) -> str:
    """Convierte un gist/archivo a texto-contexto para el análisis QA."""
    parts = [f"FUENTE: {snip.source}"]
    for name, body in snip.files.items():
        parts.append(f"\n--- {name} ---\n{body}")
    return "\n".join(parts)


def issue_to_context(issue: Issue) -> str:
    """Convierte un issue a texto-contexto para que For3s lo analice."""
    parts = [
        f"ISSUE: {issue.url}",
        f"Título: {issue.title}",
        f"Autor: {issue.author} · Estado: {issue.state}",
    ]
    if issue.labels:
        parts.append(f"Labels: {', '.join(issue.labels)}")
    if issue.body:
        parts.append(f"\nDescripción:\n{issue.body}")
    for c in issue.comments:
        parts.append(f"\n--- Comentario de {c.author} ---\n{c.body}")
    return "\n".join(parts)


def pr_to_context(pr: PullRequest) -> str:
    """Convierte el PR a texto-contexto para el análisis QA de Claude."""
    parts = [
        f"PULL REQUEST: {pr.url}",
        f"Título: {pr.title}",
        f"Autor: {pr.author} · Estado: {pr.state} · {pr.head} → {pr.base}",
        f"Cambios: +{pr.additions} −{pr.deletions} en {pr.changed_files} archivos",
    ]
    if pr.body:
        parts.append(f"Descripción:\n{pr.body}")
    if pr.omitted_files:
        parts.append(
            f"⚠️ NOTA: {pr.omitted_files} archivos fueron OMITIDOS por tamaño — "
            "decláralo en el reporte."
        )
    for f in pr.files:
        parts.append(
            f"\n--- {f.filename} ({f.status}, +{f.additions}/−{f.deletions})"
            f"{' [TRUNCADO]' if f.truncated else ''} ---\n{f.patch}"
        )
    return "\n".join(parts)
