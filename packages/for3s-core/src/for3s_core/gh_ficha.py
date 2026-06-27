"""Ficha/infografía de un repo de GitHub (pedido de diseño 2026-06-15).

La barra lateral de github.com (Languages con %, Contributors, Deployments,
About) NO se saca inspeccionando HTML — se saca de la API REST oficial de
GitHub. El MCP server (toolsets issues/pull_requests/repos) no expone tools
para languages/contributors/deployments, así que los traemos con httpx directo
usando el mismo PAT cifrado (read-only, datos públicos, API oficial).

Orden pedido por Nota: README (lo lee el análisis) → Languages% → About →
deployments. Esta ficha se antepone al reporte en TODO análisis de repo.
"""

from __future__ import annotations

import httpx

_API = "https://api.github.com"


def _headers(pat: str) -> dict:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _contar_por_link(resp: httpx.Response, items_si_no_link: int) -> int:
    """Total de elementos usando el header Link (rel=last) o el len de la página.

    GitHub pagina: si hay rel="last" con page=N y per_page=1, el total ≈ N.
    Si no hay Link, todo cupo en una página → len de los items.
    """
    link = resp.headers.get("Link", "")
    if 'rel="last"' in link:
        # ...page=NNN>; rel="last"
        import re
        m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
        if m:
            return int(m.group(1))
    return items_si_no_link


async def obtener_ficha(owner: str, repo: str, pat: str) -> dict:
    """Trae About + lenguajes% + contributors + deployments. Defensivo: cualquier
    campo que falle queda en None/[] sin tumbar el resto. Devuelve un dict.
    """
    base = f"{_API}/repos/{owner}/{repo}"
    h = _headers(pat)
    ficha: dict = {
        "descripcion": None, "homepage": None, "topics": [],
        "lenguajes": [], "lenguaje_principal": None,
        "contributors": None, "deployments": None,
        "stars": None, "forks": None, "open_issues": None,
        "default_branch": None, "license": None,
    }
    async with httpx.AsyncClient(timeout=20) as cli:
        # 1) metadata del repo (About, stars, license, topics...)
        try:
            r = await cli.get(base, headers=h)
            if r.status_code == 200:
                d = r.json()
                ficha["descripcion"] = d.get("description")
                ficha["homepage"] = d.get("homepage") or None
                ficha["topics"] = d.get("topics") or []
                ficha["stars"] = d.get("stargazers_count")
                ficha["forks"] = d.get("forks_count")
                ficha["open_issues"] = d.get("open_issues_count")
                ficha["default_branch"] = d.get("default_branch")
                lic = d.get("license")
                ficha["license"] = lic.get("spdx_id") if isinstance(lic, dict) else None
                ficha["lenguaje_principal"] = d.get("language")
        except Exception:
            pass
        # 2) lenguajes con % (bytes por lenguaje)
        try:
            r = await cli.get(f"{base}/languages", headers=h)
            if r.status_code == 200:
                langs = r.json()
                total = sum(langs.values()) or 1
                ficha["lenguajes"] = [
                    (k, round(v / total * 100, 1))
                    for k, v in sorted(langs.items(), key=lambda x: -x[1])
                ]
        except Exception:
            pass
        # 3) contributors (conteo)
        try:
            r = await cli.get(f"{base}/contributors", headers=h,
                              params={"per_page": 1, "anon": "true"})
            if r.status_code == 200:
                ficha["contributors"] = _contar_por_link(r, len(r.json()))
        except Exception:
            pass
        # 4) deployments (conteo)
        try:
            r = await cli.get(f"{base}/deployments", headers=h, params={"per_page": 1})
            if r.status_code == 200:
                ficha["deployments"] = _contar_por_link(r, len(r.json()))
        except Exception:
            pass
    return ficha


def formatear_ficha(owner: str, repo: str, ficha: dict) -> str:
    """Renderiza la ficha como cabecera del reporte. Orden: About → Lenguajes% →
    señales (contributors, deployments, stars). README lo cubre el análisis.
    """
    lineas = [f"📊 FICHA — {owner}/{repo}"]
    if ficha.get("descripcion"):
        lineas.append(f"📝 {ficha['descripcion']}")
    if ficha.get("homepage"):
        lineas.append(f"🔗 {ficha['homepage']}")
    if ficha.get("topics"):
        lineas.append("🏷️ " + ", ".join(ficha["topics"][:8]))
    if ficha.get("lenguajes"):
        langs = " · ".join(f"{k} {p}%" for k, p in ficha["lenguajes"][:6])
        lineas.append(f"🧬 {langs}")
    elif ficha.get("lenguaje_principal"):
        lineas.append(f"🧬 {ficha['lenguaje_principal']}")
    señales = []
    if ficha.get("contributors") is not None:
        señales.append(f"👥 {ficha['contributors']} contributors")
    if ficha.get("deployments") is not None:
        señales.append(f"🚀 {ficha['deployments']} deployments")
    if ficha.get("stars") is not None:
        señales.append(f"⭐ {ficha['stars']}")
    if ficha.get("open_issues") is not None:
        señales.append(f"🐛 {ficha['open_issues']} issues abiertas")
    if señales:
        lineas.append(" · ".join(señales))
    if ficha.get("license"):
        lineas.append(f"⚖️ {ficha['license']}")
    return "\n".join(lineas)
