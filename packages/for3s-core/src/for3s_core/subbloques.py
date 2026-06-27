"""Orquestador de sub-bloques por USO (Anexo R3 — control por uso, no por tiempo).

Diseño aprobado. Doc: Mente/Cuerpo/Ronda_03_Anexo_Control_Por_Uso_Subbloques.md

PROBLEMA: un repo grande no cabe en un solo loop. Mandar todo de golpe satura
el rate-limit por-minuto.

IDEA (el dueño): partir el repo en SUB-BLOQUES (= archivos) y procesarlos por USO
(esperar a que termine uno antes del siguiente), acomodando en el servidor.

LECTURA POR CATEGORÍAS (2026-06-15, pedido de diseño): el cap fijo de 25 cortaba
TODO src/ en repos como godinez-ai (65 archivos: leía 25 docs/config y se
quedaba sin cupo ANTES del código fuente → reporte sesgado). Ahora se reparte
por CATEGORÍA con cuota propia: docs, config, SRC (código — cuota grande,
SIEMPRE entra), tests, otros. Tope global alto. Nunca corta una categoría entera.

LOTES DE 2 (2026-06-15): se procesan 2 archivos a la vez (en paralelo) para ir
~2x más rápido sin disparar ráfaga grande. El control por uso pasa a ser "espera
a que el LOTE termine antes del siguiente lote".
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import httpx

from for3s_core import memory
from for3s_core.llm import ClaudeProvider, RateLimitExceeded
from for3s_core.mcp_client import GitHubMCPClient

# Tope GLOBAL de archivos (red de seguridad para monorepos gigantes). Alto a
# propósito: la cobertura real la deciden las cuotas por categoría de abajo.
# El freno real ahora es el TIEMPO (no un cap de archivos): lee lo más
# prioritario hasta agotar el presupuesto, luego cierra honesto (pedido de diseño
# 2026-06-15: donutbrowser 342 archivos tardó 12min y aún cubría solo 18% del
# código). Cap global alto solo como red de seguridad de memoria.
MAX_ARCHIVOS_TOTAL = 200
PRESUPUESTO_SEGUNDOS = 300  # 5 min: si se excede, corta y reporta lo leído

# ORDEN DE LECTURA (pedido de diseño 2026-06-17, como lee un humano experto):
#   README → config → doc → src → test → otro
# README/config/doc se leen COMPLETOS (son el cimiento del entendimiento: qué
# ES, cómo se construye, la idea). src/test se leen por RECENCIA (lo más
# recién modificado primero = lo que están construyendo AHORA = importancia real).
# None = sin cuota (completo). El tope global + presupuesto de tiempo son la
# red de seguridad para repos enormes.
# MODO PROFUNDO: lee MUCHO (el detalle). README/config completos, doc y src amplios.
CUOTA_CATEGORIA = {
    "readme": None,  # README.md — SIEMPRE completo, va PRIMERO (el mapa del repo)
    "config": None,  # package.json, pyproject, Dockerfile, CI — completo (panorama deps)
    "doc": 25,       # *.md, docs/ — completo hasta tope alto de seguridad
    "src": 120,      # CÓDIGO FUENTE — por recencia (reciente→viejo)
    "test": 10,      # tests/ — por recencia, muestra amplia
    "otro": 3,       # lo demás — mínimo
}
# MODO SIMPLE (2026-06-17): MÁXIMO contexto en MÍNIMO tiempo, para que un
# NO programador entienda qué es el proyecto, cómo va y lo esencial (+ vulns).
# README/config completos (el mapa), doc SOLO pasada de contexto (5), src SOLO
# los archivos clave (8). NO lee todo el código — eso es el profundo.
CUOTA_CATEGORIA_SIMPLE = {
    "readme": None,  # completo (el mapa)
    "config": None,  # completa (panorama de deps/stack)
    "doc": 5,        # pasada de contexto, no a detalle
    "src": 8,        # solo los archivos clave (entry/data/principales)
    "test": 1,       # solo señal de que hay tests
    "otro": 1,
}
# Orden en que se PROCESAN las categorías (= orden de lectura de el dueño)
ORDEN_CATEGORIAS = ("readme", "config", "doc", "src", "test", "otro")
# Cuántos commits recientes se revisan para ordenar src/test por recencia real
COMMITS_PARA_RECENCIA = 15

# Umbral PEQUEÑO vs GRANDE (router). <= este # de archivos → modo rápido.
UMBRAL_REPO_PEQUENO = 12
MAX_PROFUNDIDAD = 3  # subir a 3: src/components/... suele estar a 2-3 niveles
LOTE = 3  # archivos en paralelo por vuelta (más rápido; el sistema avisa si topa 429)

_IGNORAR_EXT = (
    ".lock", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".pdf", ".zip", ".tar", ".gz", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".min.js", ".min.css", ".map",
)
_IGNORAR_NOMBRE = ("__init__.py",)  # vacíos; el MCP los da como tipo resource
_IGNORAR_DIR = (
    "node_modules", ".git", "dist", "build", "vendor", "__pycache__",
    ".venv", "_generated",  # convex/_generated: código autogenerado, ruido
)

# patrones para clasificar por categoría
_EXT_CODIGO = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java",
               ".rb", ".php", ".c", ".cpp", ".h", ".cs", ".swift", ".kt", ".vue", ".svelte")
_CONFIG_HINTS = ("package.json", "pyproject.toml", "cargo.toml", "go.mod",
                 "requirements.txt", "dockerfile", "makefile", ".config.",
                 "tsconfig", "tailwind", "postcss", "vercel.json", ".yml", ".yaml", ".toml")


def _categoria(ruta: str) -> str:
    r = ruta.lower()
    nombre = r.rsplit("/", 1)[-1]
    # README de la RAÍZ va PRIMERO, categoría propia (el mapa del repo, pedido de diseño).
    if nombre in ("readme.md", "readme") and "/" not in ruta.strip("/"):
        return "readme"
    if (r.startswith("test") or "/test" in r or nombre.startswith("test_")
            or ".test." in nombre or ".spec." in nombre):
        return "test"
    if (nombre.endswith(".md") or r.startswith("docs/") or "/docs/" in r
            or nombre.startswith("readme")):
        return "doc"  # otros READMEs (de subcarpetas) y docs
    if any(h in nombre for h in _CONFIG_HINTS):
        return "config"
    if nombre.endswith(_EXT_CODIGO):
        return "src"
    return "otro"


def _peso_en_cat(ruta: str) -> int:
    """Dentro de una categoría, prioriza los archivos más informativos."""
    n = ruta.lower().rsplit("/", 1)[-1]
    claves = ("readme", "main.", "index.", "app.", "page.", "schema", "config")
    for i, c in enumerate(claves):
        if c in n:
            return i
    return len(claves) + 1


def _capa_ejecucion(ruta: str) -> int:
    """Capa de ejecución de la app, de AFUERA hacia ADENTRO (idea 2026-06-17,
    inspirada en cómo un experto lee un repo: entry → data → componentes → primitivos).
    Menor = se lee primero. Heurístico por convenciones comunes (Next/React/Node/
    Python). En stacks raros, casi todo cae en capa 4 → orden por recencia (respaldo).
    """
    r = ruta.lower()
    n = r.rsplit("/", 1)[-1]
    # CAPA 0 — entry-point (lo primero que ejecuta/renderiza la app)
    if (n in ("layout.tsx", "layout.ts", "layout.jsx", "page.tsx", "page.ts", "page.jsx")
            or n.startswith(("main.", "index.", "app.", "server.", "__main__"))
            or "app/layout" in r or "app/page" in r):
        return 0
    # CAPA 1 — fuente de verdad / datos / estado / schema
    if ("data" in n or "store" in r or "schema" in r or "/db/" in r or r.startswith("db/")
            or "models" in r or "state" in r or "context" in r):
        return 1
    # CAPA 2 — componentes de ALTO nivel (secciones, páginas, rutas, features)
    if ("sections" in r or "/pages/" in r or "/routes/" in r or "features" in r
            or "screens" in r or "views" in r):
        return 2
    # CAPA 3 — primitivos / bajo nivel (ui, utils, helpers, lib genérica)
    if ("/ui/" in r or "primitives" in r or "utils" in r or "helpers" in r
            or "/lib/" in r or "/shared/" in r or "hooks" in r):
        return 3
    # CAPA 4 — el resto del código
    return 4


async def _listar(mcp, owner, repo, path, prof):
    try:
        raw = await mcp.call_tool(
            "get_file_contents", {"owner": owner, "repo": repo, "path": path or "/"}
        )
        entradas = json.loads(raw)
    except Exception:
        return None if prof == 0 else []
    if not isinstance(entradas, list):
        return None if prof == 0 else []
    archivos: list[str] = []
    subdirs: list[str] = []
    for e in entradas:
        if not isinstance(e, dict):
            continue
        nombre = (e.get("name") or "").lower()
        ruta = e.get("path") or ""
        tipo = e.get("type")
        if tipo == "dir":
            if nombre not in _IGNORAR_DIR:
                subdirs.append(ruta)
        elif tipo == "file":
            if nombre in _IGNORAR_NOMBRE:
                continue
            if not any(nombre.endswith(ext) for ext in _IGNORAR_EXT):
                archivos.append(ruta)
    if prof < MAX_PROFUNDIDAD:
        for d in subdirs:
            sub = await _listar(mcp, owner, repo, d, prof + 1)
            if sub:
                archivos.extend(sub)
    return archivos


async def listar_archivos_repo(mcp: GitHubMCPClient, owner: str, repo: str) -> list[str] | None:
    """Lista TODAS las rutas relevantes (recursivo). None si el repo no existe."""
    archivos = await _listar(mcp, owner, repo, "", 0)
    return archivos  # sin orden ni cap aquí: el router solo cuenta; el mapeo reparte


async def orden_por_recencia(owner: str, repo: str, pat: str) -> dict[str, int]:
    """Devuelve {ruta: rango} segun qué archivos se tocaron MÁS RECIENTE
    (pedido de diseño 2026-06-17: src se lee de lo más reciente a lo más viejo,
    porque es lo que están construyendo AHORA = importancia real).

    Trae los últimos COMMITS_PARA_RECENCIA commits (REST) y los archivos que
    tocaron. rango 0 = más reciente. Archivo no visto = sin entrada (va al final).
    Es barato: llamadas a GitHub REST, NO consumen rate-limit de Claude.
    """
    if not pat:
        return {}
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{owner}/{repo}"
    ranking: dict[str, int] = {}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cli:
            r = await cli.get(f"{base}/commits", headers=headers,
                              params={"per_page": COMMITS_PARA_RECENCIA})
            if r.status_code >= 400:
                return {}
            commits = r.json()
            rango = 0
            for c in commits:
                sha = c.get("sha")
                if not sha:
                    continue
                cr = await cli.get(f"{base}/commits/{sha}", headers=headers)
                if cr.status_code >= 400:
                    continue
                for f in cr.json().get("files", []):
                    fn = f.get("filename")
                    if fn and fn not in ranking:
                        ranking[fn] = rango
                        rango += 1
    except Exception:
        return {}
    return ranking


def repartir_por_categoria(
    archivos: list[str], recencia: dict[str, int] | None = None,
    profundo: bool = False,
) -> tuple[list[str], dict]:
    """Reparte por categoría en el ORDEN DE LECTURA de el dueño (2026-06-17):
    README → config → doc → src → test → otro.

    README/config completos (cuota None). doc completo hasta tope. src/test
    ordenados por CAPA DE EJECUCIÓN (afuera→adentro). Si profundo=True, combina
    capa + recencia. Devuelve (seleccion, cobertura).
    """
    recencia = recencia or {}
    porcat: dict[str, list[str]] = {c: [] for c in ORDEN_CATEGORIAS}
    for a in archivos:
        porcat[_categoria(a)].append(a)
    seleccion: list[str] = []
    cobertura: dict[str, dict] = {}

    # Orden de src/test según el MODO (idea 2026-06-17):
    #  SIMPLE  → por CAPA de ejecución (afuera→adentro): entry → data → comp → primitivos
    #  PROFUNDO→ COMBINACIÓN: capa de ejecución Y, dentro de cada capa, recencia
    def _orden_simple(ruta: str):
        # capa primero (cómo se ejecuta la app), recencia como desempate suave
        return (_capa_ejecucion(ruta), recencia.get(ruta, 10_000), _peso_en_cat(ruta), ruta)

    def _orden_profundo(ruta: str):
        # combinación: capa Y recencia con peso parejo (lo reciente sube dentro de capa)
        return (_capa_ejecucion(ruta), recencia.get(ruta, 10_000), ruta)

    orden_codigo = _orden_profundo if profundo else _orden_simple
    # Cuotas según el MODO: profundo lee mucho; simple lee lo clave (2026-06-17)
    cuotas = CUOTA_CATEGORIA if profundo else CUOTA_CATEGORIA_SIMPLE

    for cat in ORDEN_CATEGORIAS:
        if cat in ("src", "test"):
            lst = sorted(porcat[cat], key=orden_codigo)  # por capa (+ recencia si profundo)
        else:
            lst = sorted(porcat[cat], key=lambda r: (_peso_en_cat(r), r))
        cuota = cuotas[cat]
        elegidos = lst if cuota is None else lst[:cuota]  # None = completo
        seleccion.extend(elegidos)
        cobertura[cat] = {"total": len(lst), "leidos": len(elegidos)}
    seleccion = seleccion[:MAX_ARCHIVOS_TOTAL]
    return seleccion, cobertura


async def _procesar_archivo(provider, mcp, pool, session_id, owner, repo, pregunta,
                            ruta, system, oauth, max_tokens, progreso):
    """Lee + analiza + acomoda UN archivo. Devuelve la línea de hallazgo."""
    if progreso:
        await progreso(ruta, "curso", "")
    try:
        contenido = await mcp.call_tool(
            "get_file_contents", {"owner": owner, "repo": repo, "path": ruta}
        )
    except Exception:
        if progreso:
            await progreso(ruta, "error", "no se pudo leer")
        return (f"- {ruta}: no se pudo leer", ruta, None)
    if not contenido or not contenido.strip():
        if progreso:
            await progreso(ruta, "error", "vacío / no legible")
        return (f"- {ruta}: vacío o no legible (saltado)", ruta, None)

    prompt = (
        f"Analiza este archivo del repo {owner}/{repo} (ruta: {ruta}) en el "
        f"contexto de: «{pregunta}».\n\nResponde 1-3 líneas: qué hace y lo "
        f"relevante. Si no aporta, di 'sin relevancia'.\n\n```\n{contenido[:12_000]}\n```"
    )
    if oauth:
        from for3s_core.conversation import FOR3S_ROLE
        messages = [{"role": "user", "content": f"[{FOR3S_ROLE}]\n\n{prompt}"}]
        sys_local = ""
    else:
        messages = [{"role": "user", "content": prompt}]
        sys_local = system
    try:
        data, _h = await asyncio.to_thread(
            provider.complete_with_tools, messages,
            system=sys_local, tools=None, max_tokens=max_tokens,
        )
        analisis = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
    except RateLimitExceeded:
        if progreso:
            await progreso(ruta, "error", "límite de uso")
        raise  # propagar para cortar el lote
    except Exception:
        if progreso:
            await progreso(ruta, "error", "error analizando")
        return (f"- {ruta}: error analizando", ruta, None)

    if progreso:
        await progreso(ruta, "ok", "")
    # NO guardar aquí: el guardado lo hace el loop EN ORDEN tras el gather, para
    # que gh_resources refleje el orden de lectura (los lotes paralelos guardaban
    # desordenado — fix 2026-06-17). Devolvemos contenido para guardar luego.
    return (f"- {ruta}: {analisis}", ruta, contenido)


async def analizar_repo_por_subbloques(
    provider: ClaudeProvider,
    mcp: GitHubMCPClient,
    pool,
    *,
    session_id: str,
    owner: str,
    repo: str,
    pregunta: str,
    archivos: list[str],
    system: str = "",
    oauth: bool = False,
    progreso: Callable[[str, str, str], Awaitable[None]] | None = None,
    max_tokens: int = 1024,
    profundo: bool = False,
    ya_ordenados: bool = False,
) -> str:
    """Analiza un repo GRANDE en el ORDEN de el dueño (README→config→doc→src→test).
    src/test por CAPA de ejecución (afuera→adentro); si profundo, capa + recencia.
    En lotes, por uso. Si ya_ordenados=True, usa 'archivos' tal cual sin
    re-repartir (para CONTINUAR un mapeo cortado con los archivos faltantes)."""
    if ya_ordenados:
        # continuación: los faltantes ya vienen en orden, contar su cobertura real
        seleccion = archivos
        cobertura = {}
        for a in seleccion:
            c = _categoria(a)
            # en continuación se leen TODOS los faltantes → leidos == total
            cobertura.setdefault(c, {"total": 0, "leidos": 0})
            cobertura[c]["total"] += 1
            cobertura[c]["leidos"] += 1
    else:
        # Recencia para src/test (commits recientes; barato, REST GitHub).
        recencia = await orden_por_recencia(owner, repo, getattr(mcp, "_pat", ""))
        seleccion, cobertura = repartir_por_categoria(archivos, recencia, profundo=profundo)
    n = len(seleccion)

    # Avisar el PLAN al canal (una sola vez): totales por categoría + total
    # global. El canal usa esto para pintar el progreso POR CATEGORÍA (no por
    # archivo) con un contador i/N que sube. detalle = JSON con los totales.
    if progreso:
        plan = {cat: cobertura.get(cat, {}).get("leidos", 0) for cat in CUOTA_CATEGORIA}
        # 'profundo' le dice al canal si mostrar números (profundo) o solo bolas (simple)
        await progreso("", "plan", json.dumps({"total": n, "por_cat": plan, "profundo": profundo}))

    hallazgos: list[str] = []
    corte_por_limite = False
    corte_por_tiempo = False
    idx_corte = n  # índice donde se cortó (n = no se cortó, leyó todo)
    # cuántos archivos de cada categoría se leyeron DE VERDAD (no lo planeado).
    leidos_cat: dict[str, int] = {c: 0 for c in CUOTA_CATEGORIA}
    t0 = time.monotonic()
    # procesar en LOTES (en paralelo dentro del lote; espera entre lotes). El
    # FRENO es el tiempo: antes de cada lote se chequea el presupuesto.
    for i in range(0, n, LOTE):
        if time.monotonic() - t0 > PRESUPUESTO_SEGUNDOS:
            corte_por_tiempo = True
            idx_corte = i
            break
        grupo = seleccion[i:i + LOTE]
        tareas = [
            _procesar_archivo(provider, mcp, pool, session_id, owner, repo,
                              pregunta, ruta, system, oauth, max_tokens, progreso)
            for ruta in grupo
        ]
        try:
            res = await asyncio.gather(*tareas)
            # guardar EN ORDEN (no en paralelo) → gh_resources fiel al orden de lectura
            for linea, ruta_r, contenido_r in res:
                hallazgos.append(linea)
                leidos_cat[_categoria(ruta_r)] += 1
                if contenido_r:
                    try:
                        await memory.save_gh_tool_calls(
                            pool, session_id=session_id,
                            tool_calls=[{"name": "get_file_contents",
                                         "args": {"owner": owner, "repo": repo, "path": ruta_r},
                                         "result": contenido_r}],
                        )
                    except Exception:
                        pass
        except RateLimitExceeded:
            corte_por_limite = True
            idx_corte = i
            break

    # Progreso pendiente (2026-06-17): si se cortó, guardar los archivos
    # que faltaron para que 'continúa' retome el mapeo REAL. Si terminó, limpiar.
    if corte_por_tiempo or corte_por_limite:
        faltantes = seleccion[idx_corte:]
        if faltantes:
            try:
                await memory.set_progreso_pendiente(
                    pool, session_id, owner, repo, faltantes, profundo
                )
            except Exception:
                pass
    else:
        try:
            await memory.limpiar_progreso_pendiente(pool, session_id)
        except Exception:
            pass

    # cobertura honesta = LEÍDO REAL / TOTAL del repo (no lo planeado)
    cob_lineas = []
    for cat, etiqueta in (("readme", "README"), ("config", "Config/CI"),
                          ("doc", "Documentación"), ("src", "Código fuente (src)"),
                          ("test", "Tests"), ("otro", "Otros")):
        c = cobertura.get(cat, {"total": 0, "leidos": 0})
        if c["total"] > 0:
            real = leidos_cat.get(cat, 0)
            marca = "✅" if real == c["total"] else "⚠️"
            cob_lineas.append(f"{marca} {etiqueta}: {real}/{c['total']} leídos")
    cobertura_txt = "\n".join(cob_lineas)

    cuerpo = "\n".join(hallazgos) if hallazgos else "No se obtuvieron hallazgos."
    if corte_por_limite:
        aviso_limite = (
            "\n\n⚠️ ATENCIÓN: se cortó por límite de uso de Claude — la cobertura "
            "de abajo está INCOMPLETA."
        )
    elif corte_por_tiempo:
        mins = int(PRESUPUESTO_SEGUNDOS // 60)
        aviso_limite = (
            f"\n\n⏱️ Es un repo grande: prioricé el código y los docs clave dentro "
            f"de ~{mins} min. La cobertura de abajo muestra qué tanto alcancé — lo "
            f"que falta NO fue analizado (pídeme una carpeta específica si quieres más)."
        )
    else:
        aviso_limite = ""

    if profundo:
        sintesis_prompt = (
            f"Eres For3s. Analizaste a fondo el repo {owner}/{repo} para: «{pregunta}». "
            f"COBERTURA REAL (qué tanto del repo viste):\n{cobertura_txt}\n\n"
            f"REGLA CRÍTICA DE HONESTIDAD: tu reporte DEBE reflejar esta cobertura. Si "
            f"una categoría dice menos del 100%, NO afirmes cosas sobre el código que "
            f"no leíste como si fueran hechos. Para lo no cubierto, di 'no revisado'. "
            f"Sé honesto en el CUERPO, no solo en una nota al pie.\n\n"
            f"Sintetiza un REPORTE QA técnico y a detalle (📋 resumen → arquitectura → "
            f"hallazgos por área → 🔐 vulnerabilidades/seguridad → ⚪ lo no revisado → "
            f"veredicto). Hallazgos por archivo:\n\n{cuerpo}"
        )
    else:
        # MODO SIMPLE (2026-06-17): contexto para que un NO PROGRAMADOR
        # entienda el proyecto. Máximo panorama, lenguaje claro, NO técnico-denso.
        sintesis_prompt = (
            f"Eres For3s. Diste una PASADA RÁPIDA al repo {owner}/{repo} (leíste el "
            f"README, la config y los archivos clave) para: «{pregunta}».\n\n"
            f"OBJETIVO: dar el MAYOR CONTEXTO posible del proyecto en pocas líneas, "
            f"explicado para que lo entienda CUALQUIERA — sobre todo alguien que NO es "
            f"programador. Nada de jerga innecesaria; si usas un término técnico, "
            f"explícalo en simple. La persona debe terminar sabiendo: QUÉ ES el "
            f"proyecto, PARA QUÉ sirve, CÓMO está hecho (en palabras llanas), su "
            f"ESTADO/madurez, y lo ESENCIAL que alguien debería saber.\n\n"
            f"Estructura sugerida:\n"
            f"• 🟢 Qué es (en 1-2 frases, sin tecnicismos)\n"
            f"• 🎯 Para qué sirve / qué problema resuelve\n"
            f"• 🧩 Cómo está construido (panorama, en simple)\n"
            f"• 📊 Estado del proyecto (¿activo? ¿maduro? ¿demo?)\n"
            f"• 🔐 Vulnerabilidades / riesgos de seguridad (IMPORTANTE: si viste algo "
            f"riesgoso — secretos expuestos, deps inseguras, configs abiertas — "
            f"resáltalo claro; si no viste nada evidente, dilo)\n"
            f"• 💡 Lo esencial que hay que saber\n\n"
            f"Es una vista POR ENCIMA (no a detalle — eso es el análisis profundo). "
            f"Hallazgos de lo que leíste:\n\n{cuerpo}"
        )
    if oauth:
        from for3s_core.conversation import FOR3S_ROLE
        msgs = [{"role": "user", "content": f"[{FOR3S_ROLE}]\n\n{sintesis_prompt}"}]
        sys_f = ""
    else:
        msgs = [{"role": "user", "content": sintesis_prompt}]
        sys_f = system
    try:
        data, _ = await asyncio.to_thread(
            provider.complete_with_tools, msgs, system=sys_f, tools=None, max_tokens=2048
        )
        reporte = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
    except Exception:
        reporte = f"Análisis de {owner}/{repo}:\n\n{cuerpo}"

    # Pie de cobertura con números: SOLO en profundo. En simple NO (2026-06-17).
    if profundo:
        pie = f"\n\n---\n📊 Cobertura ({n} archivos leídos):\n{cobertura_txt}"
        return reporte + aviso_limite + pie
    return reporte  # simple: sin pie de números, sin aviso de cobertura
