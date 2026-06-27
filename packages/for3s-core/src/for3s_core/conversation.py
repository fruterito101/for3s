# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""Conversación con memoria (H2) — une Agent (H1) + memoria + audit.

Orquesta el ciclo completo de un turno CON memoria persistente:
  1. asegura la sesión          (memory.ensure_session)
  2. guarda el turno del user   (memory.record_turn)
  3. reconstruye el historial   (memory.load_history) → se lo pasa al agente
  4. el agente responde         (Agent.ask_with_history)
  5. guarda la respuesta        (memory.record_turn)
  6. escribe en el audit chain  (audit.append)

Así For3s recuerda entre reinicios y cada turno queda auditado. El Agent
sigue PURO (no sabe de Postgres); la persistencia vive aquí.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import re
from datetime import UTC, datetime

import asyncpg

from for3s_core import audit, memory
from for3s_core.agent import FOR3S_ROLE, Agent
from for3s_core.cache import GitHubCache
from for3s_core.llm import LLMResponse
from for3s_core.mcp_client import GitHubMCPClient
from for3s_core.text_normalize import normalizar
from for3s_core.tool_loop import run_tool_loop

# Cache Valkey de lecturas de GitHub (2026-06-18), compartido por el
# proceso. Perezoso: se crea al primer uso. Si Valkey está caído, sus get/set
# degradan a no-op (el bot funciona igual, sin cache) — ver cache.py.
_gh_cache: GitHubCache | None = None


def _get_gh_cache() -> GitHubCache:
    global _gh_cache
    if _gh_cache is None:
        _gh_cache = GitHubCache()
    return _gh_cache


# Cuántos turnos recientes se le pasan a Claude como contexto. NO todo el
# historial (sesiones largas de 96k chars colgaban al bot). El resumen del
# historial viejo es R3/H5.
MAX_HISTORY_TURNS = 12

# Umbral de relevancia para recuerdos semánticos (H5): distancia coseno máxima
# para considerar un recuerdo "relevante". 0=idéntico, mayor=menos parecido. Por
# encima de esto el recuerdo es demasiado lejano para ayudar (y solo metería ruido).
_DIST_MAX_RECUERDO = 0.75
# Distancia MÍNIMA: por DEBAJO de esto el recuerdo es prácticamente IDÉNTICO a la
# query — es la propia pregunta del usuario (o un duplicado exacto de pruebas/
# repeticiones). Inyectarlo es ruido inútil ("recuerdo: tu misma pregunta") y
# confunde a Claude. 2026-06-19: el turno 387 falló justo por esto.
_DIST_MIN_RECUERDO = 0.05
# AI6 — TIERED por relevancia (cierra G5 recuerdos fragmentados): cuánto de cada
# recuerdo se inyecta depende de su distancia coseno (qué tan relevante es). Lo MUY
# relevante llega casi completo (no se pierde info por corte); lo lejano queda corto
# (no infla el contexto). Reemplaza el corte fijo de 300 con escalones. Conservador
# con OAuth Tier 1 (máx 700/recuerdo + tope global del bloque).
_MAX_CHARS_RECUERDO = 300  # base (recuerdos lejanos, 0.55–0.75) — compat
_CHARS_RELEVANTE_ALTO = 700  # muy relevante (dist < 0.35): casi completo
_CHARS_RELEVANTE_MEDIO = 450  # relevante medio (0.35–0.55)
_DIST_ALTA_RELEVANCIA = 0.35
_DIST_MEDIA_RELEVANCIA = 0.55
# Tope GLOBAL del bloque de recuerdos (anti-bloat real): aunque varios relevantes
# coincidan, el bloque entero no pasa de esto. Protege el contexto/tokens.
_MAX_CHARS_BLOQUE_RECUERDOS = 2500


def _chars_por_relevancia(dist: float) -> int:
    """AI6: cuántos chars inyectar de un recuerdo según su distancia (relevancia).
    Más cerca (dist baja) = más relevante = más texto. Lejano = corto."""
    if dist < _DIST_ALTA_RELEVANCIA:
        return _CHARS_RELEVANTE_ALTO
    if dist < _DIST_MEDIA_RELEVANCIA:
        return _CHARS_RELEVANTE_MEDIO
    return _MAX_CHARS_RECUERDO


# Respuestas-META del bot que NO son información real, solo ruido conversacional
# ("ya preguntaste", "no tengo registro", "no recibí contexto"…). Si una respuesta
# del assistant EMPIEZA con algo así, NO debe inyectarse como "memoria" — recuperarla
# crea un bucle de auto-contaminación (2026-06-22: causa del fallo intermitente
# de "¿qué repos analizamos?"). Se comparan en minúsculas contra el inicio del texto.
_PREFIJOS_META_RUIDO = (
    "ya pregunt",
    "eso ya lo",
    "como ya te",
    "como te dije",
    "como te conté",
    "no tengo registro",
    "no tengo en este",
    "no, de eso no",
    "honestamente, en esta",
    "no recibí contexto",
    "honestamente no",
    "siendo honesto contigo, en esta",
)


def _fecha_recuerdo(created_at) -> str:
    """Formatea la fecha de un recuerdo: absoluta + relativa, ej. '15 jun 2026,
    hace 7 días'. Da al bot el "mapa de cuándo se dijo qué" (2026-06-22:
    antes los recuerdos llegaban SIN fecha → el bot no sabía cuándo pasó algo).
    Defensiva: si no hay fecha, devuelve ''."""
    if not created_at:
        return ""
    try:
        ahora = datetime.now(UTC)
        dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        dias = (ahora - dt).days
        if dias <= 0:
            rel = "hoy"
        elif dias == 1:
            rel = "ayer"
        else:
            rel = f"hace {dias} días"
        meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
        return f"{dt.day} {meses[dt.month - 1]} {dt.year}, {rel}"
    except (AttributeError, TypeError, ValueError):
        return ""


def _fecha_hora_turno(created_at) -> str:
    """Como _fecha_recuerdo pero con HORA (para la línea de tiempo del historial,
    D-1): distingue turnos del mismo día. Ej. '25 jun 14:30 (hoy)'. Defensiva."""
    if not created_at:
        return ""
    try:
        ahora = datetime.now(UTC)
        dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        dias = (ahora - dt).days
        rel = "hoy" if dias <= 0 else ("ayer" if dias == 1 else f"hace {dias}d")
        meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
        return f"{dt.day} {meses[dt.month - 1]} {dt.hour:02d}:{dt.minute:02d} ({rel})"
    except (AttributeError, TypeError, ValueError):
        return ""


def _formatear_linea_tiempo(history: list, *, max_turnos: int = 8, max_chars: int = 90) -> str:
    """D-1 (Bloque 1, memoria híbrida): bloque de CONTEXTO que mapea CUÁNDO y QUIÉN
    dijo cada turno reciente, en orden cronológico. NO contamina los mensajes que
    van a Claude (esos quedan limpios role/content); esto es una referencia temporal
    aparte, como el bloque de memoria semántica. Da al agente el eje de TIEMPO+AUTOR
    para que se oriente por lo ÚLTIMO real y no solo por lo semánticamente parecido
    (refuerzo de diseño 2026-06-23: guiarse por fecha y autor al retomar).

    Solo incluye turnos que TENGAN fecha (los nuevos; los legados sin created_at se
    omiten). Defensiva: ante cualquier problema devuelve ''."""
    try:
        lineas = []
        for t in history[-max_turnos:]:
            cuando = _fecha_hora_turno(getattr(t, "created_at", None))
            if not cuando:
                continue  # turno legado sin fecha → no aporta al eje temporal
            quien = "Tú" if getattr(t, "role", "") == "user" else "For3s"
            texto = (getattr(t, "content", "") or "").strip().replace("\n", " ")
            if len(texto) > max_chars:
                texto = texto[:max_chars] + "…"
            lineas.append(f"• [{cuando}] {quien}: {texto}")
        if not lineas:
            return ""
        return (
            "LÍNEA DE TIEMPO DE ESTA CONVERSACIÓN (lo más reciente abajo — úsala "
            "para saber QUÉ fue lo ÚLTIMO y CUÁNDO; si te preguntan '¿en qué "
            "quedamos?' guíate por el turno más reciente, no por lo más "
            "parecido):\n" + "\n".join(lineas)
        )
    except Exception:  # noqa: BLE001 — la línea de tiempo nunca rompe el turno
        return ""


def _formatear_recuerdos(recuerdos: list) -> str:
    """Convierte los RecuerdoRelevante en un bloque de texto para el contexto de
    Claude. Filtra ruido: (1) recuerdos demasiado lejanos (dist > MAX), (2) la
    query a sí misma / duplicados exactos (dist < MIN), (3) casi-idénticos entre
    sí (dedup). Acorta cada uno. 2026-06-19: afinado tras turno conservador."""

    # 1) filtro de relevancia: ni muy lejano ni la propia pregunta repetida.
    #    1b) descartar respuestas-META del bot (ruido conversacional, no info real)
    #    → corta el bucle de auto-contaminación al repetir una pregunta (2026-06-22).
    def _es_meta(r) -> bool:
        return r.role == "assistant" and (r.content or "").strip().lower().startswith(
            _PREFIJOS_META_RUIDO
        )

    utiles = [
        r
        for r in recuerdos
        if _DIST_MIN_RECUERDO <= r.distancia <= _DIST_MAX_RECUERDO and not _es_meta(r)
    ]
    # 2) dedup: si dos recuerdos tienen el mismo texto (normalizado), dejar uno
    vistos = set()
    unicos = []
    for r in utiles:
        clave = (r.content or "").strip().lower()[:120]
        if clave and clave not in vistos:
            vistos.add(clave)
            unicos.append(r)
    if not unicos:
        return ""
    # 3) texto que INVITA a usar los recuerdos como evidencia real (no tímido):
    # antes decía "úsalos solo si aplican" → demasiado conservador, el bot ignoraba
    # lo que sabía. Ahora afirma que SÍ son cosas que pasaron de verdad.
    lineas = [
        "CONTEXTO DE TU MEMORIA — esto SÍ se habló antes con este usuario "
        "(recuperado por significado de conversaciones pasadas). Tenlo en "
        "cuenta para responder; si la pregunta es sobre lo que han hablado, "
        "estos son los datos reales:"
    ]
    # AI6 TIERED: ordenar por relevancia (dist asc) para que, si se llega al tope
    # global del bloque, lo MÁS relevante entre primero. Luego cada recuerdo se
    # corta según su relevancia (_chars_por_relevancia).
    total = 0
    for r in sorted(unicos, key=lambda x: x.distancia):
        if total >= _MAX_CHARS_BLOQUE_RECUERDOS:
            break  # tope global del bloque (anti-bloat) — no inyectar más
        quien = "Usuario" if r.role == "user" else "For3s"
        limite = _chars_por_relevancia(r.distancia)
        # respetar también lo que queda del tope global
        limite = min(limite, _MAX_CHARS_BLOQUE_RECUERDOS - total)
        texto = (r.content or "").strip().replace("\n", " ")[:limite]
        fecha = _fecha_recuerdo(getattr(r, "created_at", None))
        cuando = f" [{fecha}]" if fecha else ""
        lineas.append(f"- ({quien}{cuando}) {texto}")
        total += len(texto)
    return "\n".join(lineas)


# H6 Pieza-chat: palabras que indican una pregunta PANORÁMICA sobre lo trabajado
# (cuándo vale la pena inyectar el resumen de conceptos del grafo, no en cada turno).
_PALABRAS_PANORAMA = (
    "hemos",
    "trabajado",
    "enfocado",
    "revisado",
    "hablado",
    "temas",
    "resumen",
    "resúme",
    "resume",
    "en qué",
    "en que",
    "de qué",
    "de que",
    "qué hemos",
    "que hemos",
    "historial",
    "recuerdas",
    "acuerdas",
    "proyectos",
)


def _es_pregunta_panorama(message: str) -> bool:
    """True si el mensaje pregunta por la vista global de lo trabajado (→ conviene
    darle el resumen de conceptos del grafo consolidado por CLS, H6)."""
    m = (message or "").lower()
    return any(p in m for p in _PALABRAS_PANORAMA)


# AI5 — version-self-awareness: detector de pregunta sobre la VERSIÓN/cambios del
# agente (→ inyectar version.resumen() para que responda con datos reales, no invente).
_PALABRAS_VERSION = (
    "qué versión",
    "que version",
    "qué version",
    "que versión",
    "version eres",
    "versión eres",
    "cuándo te actualiz",
    "cuando te actualiz",
    "qué hay nuevo",
    "que hay nuevo",
    "qué hay de nuevo",
    "que tienes nuevo",
    "qué cambió",
    "que cambio",
    "qué hitos",
    "que hitos",
    "cuál es tu versión",
    "cual es tu version",
    "qué actualizaciones",
    "que actualizaciones",
    "novedades",
)


def _es_pregunta_version(message: str) -> bool:
    """True si preguntan por la versión/cambios/novedades del agente (→ inyectar
    el resumen de versión de AI5)."""
    m = (message or "").lower()
    return any(p in m for p in _PALABRAS_VERSION)


# G6 (2026-06-24): detector de "¿qué repos hemos analizado?" → inyectar la
# lista REAL desde gh_resources (el bot tenía 16 repos guardados pero solo recordaba
# 2 de su memoria semántica). Requiere mención de repos/repositorios + intención de
# listar/recordar, para no dispararse en cualquier mensaje que diga "repo".
_PALABRAS_REPOS = ("repo", "repositorio")
_PALABRAS_LISTAR = (
    "enlista",
    "enlístame",
    "lista",
    "listame",
    "lístame",
    "cuáles",
    "cuales",
    "qué repos",
    "que repos",
    "analizad",
    "analizamos",
    "revisad",
    "revisamos",
    "pasado",
    "hemos",
    "todos los",
    "recuerdas",
    "vimos",
)


def _es_pregunta_repos(message: str) -> bool:
    """True si preguntan por la lista de repos analizados (→ inyectar gh_resources)."""
    m = (message or "").lower()
    return any(p in m for p in _PALABRAS_REPOS) and any(p in m for p in _PALABRAS_LISTAR)


# D-2 (Bloque 1 memoria híbrida): frases de RETOMAR — el usuario quiere saber qué
# fue LO ÚLTIMO que trabajaron. Se evalúa sin acentos (vía _sin_acentos).
# Mezcla: (1) frases sueltas claras + (2) patrones REGEX que toleran palabras
# intermedias ("en que NOS quedamos", "donde lo dejamos AYER", "en que punto
# quedamos") — esto cubre las variantes naturales sin caer en falsos positivos.
_FRASES_RETOMAR = (
    "que estabamos haciendo",
    "que estabamos viendo",
    "en que ibamos",
    "que hicimos",
    "que habiamos hecho",
    "lo ultimo que",
    "ultima vez que",
    "retomemos",
    "retomamos",
    "continuemos donde",
    "seguimos donde",
    "recuerdame en que",
    "recapitula",
    "ponme al dia",
    "que veniamos",
    "de que hablabamos",
    "de que estabamos hablando",
    "donde ibamos",
    "resumeme lo que",
    "que llevamos",
)
# patrones flexibles: en/donde [ ... ] quedamos|dejamos  (tolera 'nos', 'lo', etc.)
_REGEX_RETOMAR = re.compile(r"\b(en|do?nde)\b[\w\s]{0,18}\b(quedamos|dejamos|ibamos|veniamos)\b")


def _sin_acentos(s: str) -> str:
    """minúsculas sin acentos (para detectar frases escritas con/sin tilde)."""
    import unicodedata

    return (
        unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode("ascii")
    )


def _es_pregunta_retomar(message: str) -> bool:
    """True si el usuario pide RETOMAR ('¿en qué quedamos?', 'en qué nos quedamos',
    'qué hicimos', 'lo último', 'ponme al día'...) → inyectar los últimos turnos
    CRUDOS por orden cronológico (D-2), para guiarse por lo ÚLTIMO real y no por lo
    semánticamente parecido (refuerzo de diseño 2026-06-23)."""
    m = _sin_acentos(message)
    return bool(_REGEX_RETOMAR.search(m)) or any(f in m for f in _FRASES_RETOMAR)


def _formatear_ultimo(history: list, *, max_turnos: int = 4, max_chars: int = 400) -> str:
    """D-2: bloque 'LO ÚLTIMO QUE TRABAJARON' — los últimos turnos textuales del
    hilo, en orden cronológico, con fecha+hora y MÁS detalle que la línea de tiempo
    de D-1 (400 chars vs 90). Crudo de la BD (lo último REAL, no por semejanza).
    Excluye el turno actual (la propia pregunta '¿en qué quedamos?'). Defensiva."""
    try:
        # el último turno del history es la pregunta actual del usuario → fuera
        previos = history[:-1] if history else []
        recientes = [t for t in previos[-max_turnos:] if getattr(t, "created_at", None)]
        if not recientes:
            return ""
        lineas = []
        for t in recientes:
            cuando = _fecha_hora_turno(getattr(t, "created_at", None))
            quien = "Tú" if getattr(t, "role", "") == "user" else "For3s"
            texto = (getattr(t, "content", "") or "").strip().replace("\n", " ")
            if len(texto) > max_chars:
                texto = texto[:max_chars] + "…"
            lineas.append(f"• [{cuando}] {quien}: {texto}")
        return (
            "LO ÚLTIMO QUE TRABAJARON EN ESTE HILO (en orden, lo más reciente "
            "abajo — esto es exactamente dónde quedaron; responde el '¿en qué "
            "quedamos?' a partir de ESTO, no de lo semánticamente parecido):\n" + "\n".join(lineas)
        )
    except Exception:  # noqa: BLE001 — nunca rompe el turno
        return ""


def _formatear_conceptos(conceptos: list) -> str:
    """Resume los conceptos consolidados del grafo (H6 CLS) para el contexto. Es la
    vista PANORÁMICA de lo trabajado — complementa los recuerdos semánticos puntuales."""
    if not conceptos:
        return ""
    lineas = [
        "RESUMEN DE TU MEMORIA CONSOLIDADA (conceptos que tu propio sistema "
        "extrajo de noche de todo el historial — esto SÍ resume lo que han "
        "trabajado juntos; úsalo si preguntan en qué se han enfocado):"
    ]
    for c in conceptos[:25]:
        label = (c.get("label") or "").strip()
        tipo = (c.get("tipo") or "").strip()
        if label:
            lineas.append(f"- {label}" + (f" ({tipo})" if tipo else ""))
    return "\n".join(lineas)


# Detector LIGERO de "¿este mensaje huele a GitHub?" → solo entonces se le dan
# las tools a Claude (corre el loop MCP). Ahorra rate-limit (el tool-use manda
# schemas pesados; ver hallazgo Paso 3) y mantiene la charla normal ágil.
#
# H-F bugfix (2026-06-14): el regex viejo NO matcheaba plurales ("issues",
# "repos", "commits") por un \b mal puesto, ni nombres de repo ("godinez-studio",
# "owner/repo"). Por eso "CUANTOS ISSUES tienen godinez-studio" caía a chat
# normal → el agente inventaba. Ahora: keywords con plural opcional + patrón
# de nombre-de-repo (palabra-palabra o owner/repo) + URL GitHub.
_GH_HINT_RE = re.compile(
    r"(github\.com"
    r"|\bpull\s*requests?\b"
    r"|\bpr\b|\bprs\b"
    r"|\bissues?\b"
    r"|\brepos?(itorios?)?\b"
    r"|\bcommits?\b"
    r"|\bbranch(es)?\b"
    r"|\bpull/\d+|\bissues?/\d+"
    r"|\bc[oó]digo\b|\barchivos?\b"
    r"|\b[a-z][\w.\-]{2,}/[\w.\-]{2,}\b"  # owner/repo (ej. owner/proyecto)
    r"|\bgodinez[\w.\-]*\b"  # repos del proyecto (godinez-studio, godinez-ai)
    r")",
    re.IGNORECASE,
)


# Directiva de tool-use (Fallo 1): el modelo a veces ANUNCIABA "déjame
# revisar..." sin EJECUTAR la tool. Esta instrucción lo corrige: usar la tool
# de inmediato y responder con el dato, sin narrar la intención.
TOOL_DIRECTIVE = (
    "\n\n[INSTRUCCIÓN CRÍTICA: tienes herramientas de GitHub disponibles. Si "
    "necesitas datos de un repo/PR/issue, LLÁMALAS AHORA directamente — NO digas "
    "'déjame revisar' ni 'voy a consultar' sin hacerlo. "
    "PROHIBIDO ABSOLUTO: NUNCA describas el contenido, stack, arquitectura o "
    "estado de un repo/PR/issue que NO hayas leído con una herramienta en ESTE "
    "turno. Si no ejecutaste la herramienta, NO inventes nada — di claramente "
    "que no pudiste traer el dato. Inventar contenido de un repo es el peor error "
    "posible. Solo afirma lo que la herramienta te devolvió de verdad. "
    "Si una herramienta ya te dio el resultado, úsalo para responder.\n"
    "PARA ANALIZAR UN REPO COMPLETO (cuando dan github.com/owner/repo sin un "
    "PR/issue específico): 1) lee el README con get_file_contents (path "
    "'README.md'); 2) lista SOLO los issues/PRs RECIENTES — usa per_page=10 y "
    "page=1, NUNCA pidas todas las páginas ni miles de resultados (repos grandes "
    "como aider tienen miles → eso satura el límite). Con 10 recientes basta para "
    "el panorama. 3) NO hagas más de 1 list_issues + 1 list_pull_requests por "
    "análisis. Luego entrega: qué ES el proyecto, su stack, actividad reciente y "
    "estado general. Si el usuario quiere MÁS detalle de algo, que lo pida después. "
    "NUNCA describas el repo sin haber leído al menos su README.\n"
    "PARA CONTAR (cuántos PRs/issues hay, por estado: abiertos/cerrados/merged): "
    "usa search_pull_requests (para PRs) o search_issues (para issues) con una "
    "query tipo 'repo:owner/nombre is:closed' (o is:open / is:merged) y perPage=1, "
    "y LEE el campo total_count de la respuesta — ese es el número EXACTO y lo da "
    "en UNA sola llamada. NUNCA cuentes paginando con list_pull_requests/"
    "list_issues (se queda corto en repos grandes y satura el límite). Si la "
    "búsqueda devuelve un error de validación (repo movido/privado), dilo honesto.\n"
    "PARA ESCRIBIR EN GITHUB (solo si el usuario lo PIDE explícitamente): puedes "
    "PROPONER add_issue_comment (comentar), create_issue (crear issue), "
    "create_pull_request (crear PR) o create_pull_request_review (review). Al "
    "llamarlas NO se ejecutan de inmediato: el sistema le mostrará al usuario un "
    "botón de confirmación. Tú solo propón la acción con los datos correctos y di "
    "en UNA frase qué vas a hacer; el usuario confirmará. NUNCA digas que ya "
    "comentaste/creaste algo: solo se hace tras su confirmación. NO puedes "
    "mergear, borrar repos, ni modificar archivos (esas no están permitidas).]"
)


# URLs dentro del mensaje. Para huele_a_github: las URLs NO-github se QUITAN antes
# de evaluar, porque el patrón "owner/repo" del hint da FALSOS POSITIVOS con paths
# de dominios web (2026-06-19: "tvazteca.com/aztecadeportes" se tomaba como
# un repo → el mensaje se desviaba al flujo GitHub en vez del web y no se leía la
# página). Las URLs de github.com SÍ se conservan (esas SÍ queremos detectarlas).
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _quitar_urls_no_github(text: str) -> str:
    """Reemplaza por espacio las URLs que NO son de github.com; deja las de
    github.com intactas. Así el detector no confunde un path web con owner/repo."""

    def _sub(m: re.Match) -> str:
        url = m.group(0)
        return url if "github.com" in url.lower() else " "

    return _URL_RE.sub(_sub, text)


def huele_a_github(text: str) -> bool:
    """True si el mensaje parece referirse a GitHub/código → activar tools.

    Normaliza primero (minúsculas + sin acentos) → robusto ante MAYÚSCULAS,
    minúsculas o InTeRcAlAdO. El regex igual usa IGNORECASE como respaldo.

    ANTES de evaluar, quita las URLs no-GitHub (fix falso positivo, el dueño
    2026-06-19): el patrón owner/repo confundía 'dominio.com/path' con un repo.
    """
    return bool(_GH_HINT_RE.search(normalizar(_quitar_urls_no_github(text))))


# ============================================================================
# Control por USO — repo enorme en sub-bloques (Anexo R3, aprobado 2026-06-14).
# Detecta "analiza el repo completo" (URL github.com/owner/repo SIN /pull ni
# /issues) y lo procesa archivo-por-archivo en fila de 1 (subbloques.py).
# ============================================================================

# github.com/owner/repo SIN una ruta de PR/issue/blob/commit detrás → "repo completo"
_REPO_COMPLETO_RE = re.compile(
    r"github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?"
    r"(?:\s|$|[)\].,])",  # termina ahí (no /pull/, /issues/, /blob/, etc.)
    re.IGNORECASE,
)
# si la URL trae alguno de estos, NO es "repo completo" (es un recurso puntual)
_SUBRECURSO_RE = re.compile(
    r"github\.com/[\w.\-]+/[\w.\-]+/(pull|issues|blob|tree|commit|releases|actions)/",
    re.IGNORECASE,
)


def extraer_owner_repo(text: str) -> tuple[str, str] | None:
    """Devuelve (owner, repo) si el texto trae una URL github.com/owner/repo
    de REPO COMPLETO (sin apuntar a un PR/issue/archivo concreto), o None."""
    if _SUBRECURSO_RE.search(text):
        return None  # apunta a un recurso puntual → no es "repo completo"
    m = _REPO_COMPLETO_RE.search(text + " ")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # filtrar rutas reservadas de github que no son owners
    if owner.lower() in ("orgs", "settings", "marketplace", "features", "about"):
        return None
    return owner, repo


# github.com/NOMBRE (org o usuario) SIN un segundo segmento /repo → "organización".
# Ej: github.com/All-Hands-AI  → org "All-Hands-AI". Pero github.com/foo/bar NO
# (eso es un repo, lo maneja extraer_owner_repo).
_ORG_RE = re.compile(
    r"github\.com/([\w.\-]+)/?(?:\s|$|[)\].,])",  # solo UN segmento tras github.com/
    re.IGNORECASE,
)
_RESERVADOS_GH = (
    "orgs",
    "settings",
    "marketplace",
    "features",
    "about",
    "pulls",
    "issues",
    "notifications",
)


def extraer_org(text: str) -> str | None:
    """Devuelve el nombre de la ORG/usuario si el texto trae github.com/NOMBRE
    SIN un /repo detrás (es una organización, no un repo), o None.

    Causa del bot mudo 2026-06-15: con una URL de org, el flujo tool-use hacía
    que Claude ALUCINARA un repo inexistente y se colgara. Detectarla evita eso.
    """
    # si ya es un repo (owner/repo) o un sub-recurso, NO es una org "suelta"
    if extraer_owner_repo(text) is not None or _SUBRECURSO_RE.search(text):
        return None
    m = _ORG_RE.search(text + " ")
    if not m:
        return None
    nombre = m.group(1)
    if nombre.lower() in _RESERVADOS_GH:
        return None
    return nombre


class Conversation:
    """Una conversación persistente atada a una sesión."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        agent: Agent,
        session_id: str,
        channel: str = "cli",
        *,
        telegram_user_id: int | None = None,
        scope_user_id: int | None = None,
    ) -> None:
        self._pool = pool
        self._agent = agent
        self._session_id = session_id
        self._channel = channel
        # #6 hilo por usuario: QUIÉN es el autor de los turnos de esta conversación
        # (user_id de Telegram). Se graba en cada record_turn para trazar al autor
        # (#3). None = CLI/legado. El session_id YA es por-persona (lo arma el canal).
        self._telegram_user_id = telegram_user_id
        # AI1 DOCTRINA DE AISLAMIENTO (defensa real, 2ª capa sobre el session_id):
        # scope con el que se filtra la búsqueda semántica. El canal lo decide: None
        # para el DUEÑO (ve todo lo suyo, compat con su legado owner_user_id=NULL);
        # el user_id para MIEMBROS (solo su privada + la común, nunca lo de otro).
        # Cierra el hueco "el scope existía (S10c) pero no se aplicaba en el flujo".
        self._scope_user_id = scope_user_id
        # Si el último send_with_tools propuso una write tool (comentar/crear),
        # queda aquí {name, args} para que el canal muestre el botón de confirmar.
        # None = no hay nada pendiente. (LLMResponse es frozen → no se puede
        # colgar ahí; lo exponemos como atributo de la conversación.)
        self.accion_pendiente: dict | None = None
        # Pieza B (H5): tareas de embedding en background. Retenemos referencia
        # para que el garbage collector NO las mate antes de terminar (asyncio
        # solo guarda weakrefs de las tasks → sin esto podrían desaparecer).
        self._bg_tasks: set = set()

    def _embeber_bg(self, seq: int, content: str) -> None:
        """Dispara el embedding de un turno en BACKGROUND (fire-and-forget). NO
        espera ni bloquea: la respuesta del bot sale ya; el embedding se calcula
        aparte (~3s) y se guarda cuando esté. Defensivo: embeddear_turno traga
        sus errores. Si no hay event loop (ej. contexto CLI sync), lo ignora."""
        try:
            import asyncio

            t = asyncio.create_task(
                memory.embeddear_turno(self._pool, self._session_id, seq, content)
            )
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            pass  # sin event loop corriendo → se embeberá por backfill después

    async def _guardar_turno(
        self,
        *,
        role: str,
        content: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str | None = None,
    ) -> int:
        """Guarda un turno Y dispara su embedding en background (Pieza B, H5).
        Envuelve memory.record_turn + _embeber_bg en uno solo → TODOS los flujos
        (send, send_with_tools, analizar_repo, continuar, listar_org) embeben sus
        turnos sin olvidar ninguno. Devuelve el seq (igual que record_turn)."""
        seq = await memory.record_turn(
            self._pool,
            self._session_id,
            role=role,
            content=content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            channel=self._channel,
            telegram_user_id=self._telegram_user_id,
        )
        self._embeber_bg(seq, content)
        return seq

    async def history(self) -> list[memory.Turn]:
        return await memory.load_history(self._pool, self._session_id)

    async def send(
        self,
        message: str,
        *,
        max_tokens: int = 1024,
        prompt: str | None = None,
        contexto: str = "",
        adjuntos: list[dict] | None = None,
    ) -> LLMResponse:
        """Procesa un turno.

        message: lo que se GUARDA en memoria (el texto original del usuario, corto).
        prompt:  lo que se MANDA a Claude (puede ser enriquecido, ej. PR completo).
                 Si es None, se manda el mismo `message`.
        Separarlos evita guardar prompts gigantes (contexto de PR de 100k chars)
        en la memoria, que luego inflaban el historial y colgaban al bot.

        adjuntos: bloques multimodales (imagen/PDF/texto de Word/Excel) que el
        usuario mandó con este turno (2026-06-18). Se mandan a Claude, NO
        se guardan en memoria (el base64 es enorme; en memoria queda solo una
        nota de texto que arma el caller).
        """
        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)

        # 1) guardar SOLO el mensaje original (corto) + audit
        # _guardar_turno guarda Y embebe en background (Pieza B). NO bloquea.
        await self._guardar_turno(role="user", content=message)

        # P1 CAPTURA EXPLÍCITA: si el mensaje es una afirmación de identidad ("soy
        # backend", "prefiero X"), guardarla en el perfil de esta persona. DEFENSIVO.
        if self._telegram_user_id is not None:
            try:
                from for3s_core.perfil import PerfilStore, detectar_afirmacion

                af = detectar_afirmacion(message)
                if af:
                    ps = PerfilStore(self._pool)
                    if "rol" in af:
                        await ps.set_campo(self._telegram_user_id, "rol", af["rol"])
                    elif "rasgo" in af:
                        await ps.add_rasgo(self._telegram_user_id, af["rasgo"])
            except Exception:  # noqa: BLE001 — captura de perfil nunca rompe el turno
                pass
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "chars": len(message)},
        )

        # 2) reconstruir historial — solo los ÚLTIMOS N turnos (no todo).
        history = await memory.load_history(self._pool, self._session_id, last_n=MAX_HISTORY_TURNS)
        prior = [{"role": t.role, "content": t.content} for t in history]
        # el último turno (el del usuario) se reemplaza por el prompt enriquecido
        # SOLO para mandárselo a Claude — en memoria queda el mensaje corto.
        if prompt is not None and prior:
            prior[-1] = {"role": "user", "content": prompt}

        # 2b) MEMORIA SEMÁNTICA (H5 sub-paso 8): además de los últimos 12 turnos,
        # buscar por SIGNIFICADO recuerdos relevantes de TODO el historial y
        # añadirlos al contexto. excluir_ultimos=MAX_HISTORY_TURNS evita duplicar
        # lo que ya entra por la ventana reciente. La query es `message` (corto),
        # NO el prompt enriquecido (respeta la separación memoria/Claude).
        # DEFENSIVO: buscar_semantico degrada a [] si el modelo falla → no rompe.
        # solo_usuario=False (2026-06-22): incluir TAMBIÉN las respuestas del
        # assistant. Con solo_usuario=True el bot recuperaba sus PROPIAS preguntas
        # viejas (que son lo más parecido a una pregunta) en vez de las respuestas
        # con la info real (repos, hallazgos) → "¿qué repos analizamos?" fallaba.
        # El bucle de auto-confirmación que motivó solo_usuario=True ya lo cortan
        # los OTROS filtros de _formatear_recuerdos (dist-min query-a-sí-misma + dedup).
        contexto_final = contexto

        # 2a-bis) LÍNEA DE TIEMPO (D-1, Bloque 1 memoria híbrida): bloque que mapea
        # CUÁNDO y QUIÉN dijo cada turno reciente. Da al agente el eje de tiempo+autor
        # para orientarse por lo ÚLTIMO real (no solo por lo semánticamente parecido).
        # NO contamina `prior` (los mensajes a Claude quedan limpios). Defensivo.
        linea_tiempo = _formatear_linea_tiempo(history)
        if linea_tiempo:
            contexto_final = (
                f"{contexto_final}\n\n{linea_tiempo}" if contexto_final else linea_tiempo
            )

        # 2a-ter) RETOMAR (D-2): si el usuario pregunta '¿en qué quedamos?' / 'qué
        # hicimos' / 'ponme al día', inyectar los últimos turnos CRUDOS con detalle
        # (400 chars) por orden cronológico — lo ÚLTIMO REAL, no lo parecido. Es el
        # foco que complementa la línea de tiempo (panorama). Defensivo.
        if _es_pregunta_retomar(message):
            ultimo = _formatear_ultimo(history)
            if ultimo:
                contexto_final = f"{contexto_final}\n\n{ultimo}" if contexto_final else ultimo

        # DOS búsquedas combinadas (2026-06-22): las preguntas del usuario se
        # parecen entre sí pero NO traen info; las RESPUESTAS del bot sí (repos,
        # hallazgos, datos). Buscar solo_asistente garantiza que entre la INFO real;
        # la búsqueda general aporta contexto adicional. Se combinan y deduplican.
        recuerdos_info = await memory.buscar_semantico(
            self._pool,
            self._session_id,
            message,
            top_n=3,
            excluir_ultimos=MAX_HISTORY_TURNS,
            solo_asistente=True,
            scope_user_id=self._scope_user_id,  # AI1: aislamiento por persona
        )
        recuerdos_gral = await memory.buscar_semantico(
            self._pool,
            self._session_id,
            message,
            top_n=3,
            excluir_ultimos=MAX_HISTORY_TURNS,
            solo_usuario=False,
            scope_user_id=self._scope_user_id,  # AI1: aislamiento por persona
        )
        # info (respuestas) primero — es lo más valioso; _formatear_recuerdos dedup
        recuerdos = recuerdos_info + recuerdos_gral
        if recuerdos:
            bloque = _formatear_recuerdos(recuerdos)
            contexto_final = f"{contexto}\n\n{bloque}" if contexto else bloque

        # 2c) GRAFO DE CONCEPTOS (H6): si la pregunta es PANORÁMICA ("¿en qué nos
        # hemos enfocado?"), inyectar el resumen de conceptos que CLS consolidó de
        # noche. Complementa los recuerdos puntuales con la vista global. DEFENSIVO:
        # si el grafo falla, se ignora (no rompe el turno).
        if _es_pregunta_panorama(message):
            try:
                from for3s_core import kg

                conceptos = await kg.conceptos(self._pool)
                bloque_kg = _formatear_conceptos(conceptos)
                if bloque_kg:
                    contexto_final = (
                        f"{contexto_final}\n\n{bloque_kg}" if contexto_final else bloque_kg
                    )
            except Exception:  # noqa: BLE001 — grafo secundario, nunca rompe el turno
                pass

        # 2d) AI5 version-self-awareness: si preguntan por la versión/cambios del
        # agente, inyectar el resumen de versión (datos reales) para que NO invente.
        if _es_pregunta_version(message):
            try:
                from for3s_core import version as _ver

                try:
                    sv = await self._pool.fetchval("SELECT max(version) FROM schema_version")
                except Exception:  # noqa: BLE001 — schema_version opcional
                    sv = None
                bloque_ver = _ver.resumen(sv)
                contexto_final = (
                    f"{contexto_final}\n\n{bloque_ver}" if contexto_final else bloque_ver
                )
            except Exception:  # noqa: BLE001 — versión secundaria, nunca rompe el turno
                pass

        # 2e) AI4 AUTO-RETOMAR: si la persona vuelve a este hilo tras inactividad
        # (>~3h), inyectar el STATUS curado ("en qué quedamos") generado de noche.
        # En conversación activa NO se inyecta (los 12 turnos bastan). DEFENSIVO.
        try:
            from for3s_core import hilo_status

            st = await hilo_status.debe_inyectar(self._pool, self._session_id)
            if st:
                bloque_st = (
                    "RETOMANDO ESTE HILO (resumen de en qué quedaron la última vez — "
                    "úsalo para dar continuidad natural, NO lo recites literal):\n" + st
                )
                contexto_final = f"{contexto_final}\n\n{bloque_st}" if contexto_final else bloque_st
        except Exception:  # noqa: BLE001 — auto-retomar secundario, nunca rompe el turno
            pass

        # 2f) G6: si preguntan por la lista de repos analizados, inyectar la lista
        # REAL desde gh_resources (no solo lo que esté en memoria semántica/grafo,
        # que puede ser parcial). Cierra "tenía 16 repos pero recordó 2". DEFENSIVO.
        if _es_pregunta_repos(message):
            try:
                repos = await memory.repos_analizados(self._pool, self._session_id)
                if repos:
                    lista = "\n".join(f"- {o}/{r}" for o, r in repos)
                    bloque_repos = (
                        "REPOS QUE HAS ANALIZADO EN ESTE HILO (lista REAL y completa de "
                        "tu registro de GitHub — úsala para responder; estos son TODOS, "
                        "no inventes ni omitas):\n" + lista
                    )
                    contexto_final = (
                        f"{contexto_final}\n\n{bloque_repos}" if contexto_final else bloque_repos
                    )
            except Exception:  # noqa: BLE001 — lista de repos secundaria, no rompe
                pass

        # 2g) P1 PERFIL DE USUARIO: inyectar el perfil de QUIEN habla (rol, stack,
        # estilo, rasgos) para adaptar la respuesta a quién es. Va en CADA turno con
        # autor (es "quién eres", relevante siempre). DEFENSIVO: si falla, se ignora.
        if self._telegram_user_id is not None:
            try:
                from for3s_core.perfil import PerfilStore

                perfil_txt = await PerfilStore(self._pool).resumen(self._telegram_user_id)
                if perfil_txt:
                    contexto_final = (
                        f"{contexto_final}\n\n{perfil_txt}" if contexto_final else perfil_txt
                    )
            except Exception:  # noqa: BLE001 — perfil secundario, nunca rompe el turno
                pass

        # 2h) H10 SKILLS: si alguna skill (receta) APLICA al mensaje, inyectar su
        # SKILL.md al contexto para que el bot siga la receta. Carga progresiva: solo
        # se trae el contenido de la(s) skill(s) que coinciden. DEFENSIVO: si falla,
        # se ignora. Registra el uso (para la curación nocturna de H11/H12).
        try:
            from for3s_core.skills import SkillStore

            ss = SkillStore(self._pool)
            relevantes = await ss.buscar_relevantes(message, limite=2)
            if relevantes:
                bloques = []
                for sk in relevantes:
                    full = await ss.ver(sk.nombre, categoria=sk.categoria)
                    if full:
                        bloques.append(full["contenido"][:1500])
                        await ss.registrar_uso(sk.id)
                if bloques:
                    cuerpo_sk = "\n\n---\n\n".join(bloques)
                    bloque_sk = (
                        "SKILL(S) QUE APLICAN A ESTO (recetas reutilizables — sigue sus "
                        "pasos, son tu conocimiento procedimental):\n\n" + cuerpo_sk
                    )
                    contexto_final = (
                        f"{contexto_final}\n\n{bloque_sk}" if contexto_final else bloque_sk
                    )
        except Exception:  # noqa: BLE001 — skills secundario, nunca rompe el turno
            pass

        # 3) el agente responde. ask_with_history es SÍNCRONO (httpx bloqueante);
        # to_thread libera el event loop → el bot no se congela y el wait_for
        # del canal SÍ puede cortar (bug del PR #134).
        resp = await asyncio.to_thread(
            functools.partial(
                self._agent.ask_with_history,
                prior,
                max_tokens=max_tokens,
                contexto=contexto_final,
                adjuntos=adjuntos,
            )
        )

        # 3b) H10-PLANEA metacognición ("sé cuándo no sé"): evalúo mi confianza en
        # esta respuesta. Si es BAJA y el texto NO expresó ya su incertidumbre, añado
        # una línea de transparencia (no invento seguridad). Registra el confidence en
        # el audit (observabilidad). DEFENSIVO: nunca rompe el turno.
        try:
            from for3s_core import confidence as _cf

            score = await _cf.evaluar_respuesta_chat(
                self._pool, respuesta=resp.text, session_id=self._session_id
            )
            if score.nivel == _cf.ConfidenceLevel.CRITICAL:
                # ¿el texto ya fue honesto sobre su duda? (la señal 1 lo mide)
                ya_honesto = any(
                    s.nombre == "llm_self_report" and s.valor < 0.65 for s in score.señales
                )
                if not ya_honesto:
                    nota = (
                        "\n\n_⚠️ Nota: no estoy del todo seguro de esto — "
                        "verifícalo o dame más contexto si es importante._"
                    )
                    resp = resp.__class__(
                        text=resp.text + nota,
                        input_tokens=resp.input_tokens,
                        output_tokens=resp.output_tokens,
                        model=resp.model,
                        usage_5h=getattr(resp, "usage_5h", None),
                        usage_7d=getattr(resp, "usage_7d", None),
                    )
        except Exception:  # noqa: BLE001 — metacognición secundaria, nunca rompe el turno
            pass

        # 4) guardar respuesta + audit (guarda + embebe en bg, Pieza B)
        await self._guardar_turno(
            role="assistant",
            content=resp.text,
            tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens,
            model=resp.model,
        )
        await audit.append(
            self._pool,
            actor="for3s",
            action="message_out",
            detail={
                "session": self._session_id,
                "tokens_in": resp.input_tokens,
                "tokens_out": resp.output_tokens,
                "model": resp.model,
            },
        )
        return resp

    async def send_with_tools(
        self, message: str, mcp: GitHubMCPClient, *, max_tokens: int = 2048
    ) -> LLMResponse:
        """Turno CON tools de GitHub (migración MCP, Paso 4-6).

        Como send() pero deja que el MODELO decida usar las tools GitHub (vía
        MCP), en vez del regex artesanal. Reusa memoria + audit. El system es
        FOR3S_ROLE; en modo OAuth va antepuesto al mensaje (la suscripción
        rechaza system custom). Persiste lo que las tools traen (gh_resources).
        """
        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)

        # 1) guardar el mensaje del usuario + audit
        await self._guardar_turno(role="user", content=message)
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "chars": len(message)},
        )

        # 2) historial reciente → messages[] formato Anthropic
        history = await memory.load_history(self._pool, self._session_id, last_n=MAX_HISTORY_TURNS)
        messages = [{"role": t.role, "content": t.content} for t in history]

        # OAuth: la identidad For3s va en el system de Claude Code + el rol en
        # el último mensaje (la suscripción no admite system custom). En API key
        # el rol va como system del loop.
        provider = self._agent._provider
        oauth = getattr(self._agent, "_oauth", False)
        if oauth:
            system = ""
            contenido = f"[{FOR3S_ROLE}]\n\n{message}{TOOL_DIRECTIVE}"
            if messages and messages[-1]["role"] == "user":
                messages[-1] = {"role": "user", "content": contenido}
            else:
                messages.append({"role": "user", "content": contenido})
        else:
            system = FOR3S_ROLE
            if messages and messages[-1]["role"] == "user":
                messages[-1] = {"role": "user", "content": f"{message}{TOOL_DIRECTIVE}"}

        # 3) correr el loop de tool-use (el modelo decide usar GitHub o no).
        # cache: las lecturas cacheables de GitHub se sirven de Valkey si hay hit.
        # workspace_id = la sesión (hoy "brian"); cuando llegue multi-tenant, ya
        # queda namespaced sin reescribir nada.
        result = await run_tool_loop(
            provider,
            mcp,
            messages,
            system=system,
            max_tokens=max_tokens,
            cache=_get_gh_cache(),
            workspace_id=self._session_id,
        )
        # Si el agente PROPUSO una write (comentar/crear), la dejamos para que el
        # canal muestre el botón de confirmación. NO se ejecutó nada todavía.
        self.accion_pendiente = result.accion_pendiente

        # 4) persistir lo que las tools trajeron de GitHub (Paso 4)
        if result.tool_calls:
            await memory.save_gh_tool_calls(
                self._pool,
                session_id=self._session_id,
                tool_calls=result.tool_calls,
            )

        # 5) guardar respuesta + audit (el reporte, no el contexto crudo)
        await self._guardar_turno(
            role="assistant",
            content=result.text,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            model=result.model,
        )
        await audit.append(
            self._pool,
            actor="for3s",
            action="message_out",
            detail={
                "session": self._session_id,
                "tokens_in": result.input_tokens,
                "tokens_out": result.output_tokens,
                "model": result.model,
                "tools": [tc["name"] for tc in result.tool_calls],
            },
        )

        return LLMResponse(
            text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model=result.model,
            usage_5h=result.usage_5h,
            usage_7d=result.usage_7d,
        )

    async def analizar_repo_completo(
        self,
        message: str,
        mcp: GitHubMCPClient,
        owner: str,
        repo: str,
        *,
        progreso=None,
        profundo: bool = False,
    ) -> LLMResponse:
        """Analiza un repo. ROUTER (2026-06-15): primero lista los archivos; si
        el repo es PEQUEÑO (<= UMBRAL) usa el modo RÁPIDO (send_with_tools, 1-2
        llamadas); si es GRANDE usa el mapeo por sub-bloques (Anexo R3, fila de 1).

        Así un repo chico no paga el costo del mapeo completo y responde rápido,
        y uno grande no satura el rate-limit (lo procesa archivo por archivo).

        progreso(ruta, estado, detalle): callback async; estado ∈ {curso,ok,error}.
        Solo se usa en el modo GRANDE (el rápido no tiene progreso por archivo).
        """
        from for3s_core import gh_ficha, subbloques

        provider = self._agent._provider
        oauth = getattr(self._agent, "_oauth", False)
        system = "" if oauth else FOR3S_ROLE

        # 1) listar archivos (1 llamada barata) → decide pequeño vs grande
        archivos = await subbloques.listar_archivos_repo(mcp, owner, repo)

        # repo inaccesible (no existe / privado / sin acceso)
        if archivos is None:
            texto = (
                f"No pude listar `{owner}/{repo}`. ¿Existe y el token tiene acceso? "
                f"(puede ser privado, o el nombre estar mal escrito)."
            )
            await memory.ensure_session(self._pool, self._session_id, channel=self._channel)
            await self._guardar_turno(role="user", content=message)
            await self._guardar_turno(role="assistant", content=texto)
            return LLMResponse(text=texto, input_tokens=0, output_tokens=0, model=provider._model)

        # FICHA del repo (infografía: About + lenguajes% + contributors +
        # deployments). Se antepone al reporte en AMBOS modos (pedido de diseño
        # 2026-06-15). Defensiva: si falla, sigue sin ficha.
        try:
            _f = await gh_ficha.obtener_ficha(owner, repo, getattr(mcp, "_pat", ""))
            ficha_txt = gh_ficha.formatear_ficha(owner, repo, _f)
        except Exception:
            ficha_txt = ""

        # 2) ROUTER: repo PEQUEÑO → modo rápido (send_with_tools). Reusa todo el
        #    flujo tool-use ya probado; el modelo lee README + lista en 1-2 vueltas.
        if len(archivos) <= subbloques.UMBRAL_REPO_PEQUENO:
            resp = await self.send_with_tools(message, mcp, max_tokens=2048)
            if ficha_txt:
                resp = dataclasses.replace(resp, text=f"{ficha_txt}\n\n---\n\n{resp.text}")
            return resp

        # 3) repo GRANDE → mapeo por sub-bloques (fila de 1, progreso editable)
        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)
        await self._guardar_turno(role="user", content=message)
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "chars": len(message), "modo": "repo_grande"},
        )

        reporte = await subbloques.analizar_repo_por_subbloques(
            provider,
            mcp,
            self._pool,
            session_id=self._session_id,
            owner=owner,
            repo=repo,
            pregunta=message,
            archivos=archivos,
            system=system,
            oauth=oauth,
            progreso=progreso,
            profundo=profundo,
        )

        if ficha_txt:
            reporte = f"{ficha_txt}\n\n---\n\n{reporte}"

        await self._guardar_turno(role="assistant", content=reporte)
        await audit.append(
            self._pool,
            actor="for3s",
            action="message_out",
            detail={
                "session": self._session_id,
                "modo": "repo_grande",
                "owner": owner,
                "repo": repo,
                "archivos": len(archivos),
            },
        )
        return LLMResponse(text=reporte, input_tokens=0, output_tokens=0, model=provider._model)

    async def continuar_repo_pendiente(
        self, message: str, mcp: GitHubMCPClient, pendiente: dict, *, progreso=None
    ) -> LLMResponse:
        """Continúa un mapeo que se CORTÓ por tiempo (2026-06-17): lee los
        archivos que FALTARON de verdad (no improvisa), con marcador. pendiente =
        {owner, repo, faltantes, profundo} guardado en sessions.meta."""
        from for3s_core import subbloques

        owner = pendiente["owner"]
        repo = pendiente["repo"]
        faltantes = pendiente.get("faltantes", [])
        profundo = pendiente.get("profundo", False)
        provider = self._agent._provider
        oauth = getattr(self._agent, "_oauth", False)
        system = "" if oauth else FOR3S_ROLE

        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)
        await self._guardar_turno(role="user", content=message)
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "modo": "continuar", "repo": repo},
        )

        reporte = await subbloques.analizar_repo_por_subbloques(
            provider,
            mcp,
            self._pool,
            session_id=self._session_id,
            owner=owner,
            repo=repo,
            pregunta=f"continúa el análisis de lo que faltó en {owner}/{repo}",
            archivos=faltantes,
            system=system,
            oauth=oauth,
            progreso=progreso,
            profundo=profundo,
            ya_ordenados=True,
        )

        await self._guardar_turno(role="assistant", content=reporte)
        await audit.append(
            self._pool,
            actor="for3s",
            action="message_out",
            detail={"session": self._session_id, "modo": "continuar", "repo": repo},
        )
        return LLMResponse(text=reporte, input_tokens=0, output_tokens=0, model=provider._model)

    async def listar_repos_org(self, message: str, mcp: GitHubMCPClient, org: str) -> LLMResponse:
        """La URL apunta a una ORGANIZACIÓN (github.com/NOMBRE sin /repo).

        En vez de dejar que el flujo tool-use alucine un repo inexistente (causa
        del cuelgue 2026-06-15), listamos los repos de la org (search_repositories
        query 'org:NOMBRE') y preguntamos cuál analizar.
        """
        import json as _json

        await memory.ensure_session(self._pool, self._session_id, channel=self._channel)
        await self._guardar_turno(role="user", content=message)
        await audit.append(
            self._pool,
            actor="user",
            action="message_in",
            detail={"session": self._session_id, "chars": len(message), "modo": "org"},
        )

        provider = self._agent._provider
        repos: list[str] = []
        try:
            raw = await mcp.call_tool(
                "search_repositories",
                {"query": f"org:{org}", "perPage": 30, "sort": "updated"},
            )
            data = _json.loads(raw)
            items = (
                data.get("items", [])
                if isinstance(data, dict)
                else (data if isinstance(data, list) else [])
            )
            for it in items:
                nombre = it.get("name") if isinstance(it, dict) else None
                if nombre:
                    repos.append(nombre)
        except Exception:
            repos = []

        if not repos:
            texto = (
                f"`{org}` parece una organización/usuario de GitHub, pero no pude "
                f"listar sus repos (puede no existir, ser privada, o no tener repos "
                f"públicos). Dame la URL completa de un repo: `github.com/{org}/<repo>`."
            )
        else:
            lista = "\n".join(f"• `{org}/{r}`" for r in repos[:25])
            extra = f"\n…y {len(repos) - 25} más." if len(repos) > 25 else ""
            texto = (
                f"📦 `{org}` es una **organización** de GitHub con {len(repos)} repos. "
                f"No es un repo en sí — dime cuál analizo:\n\n{lista}{extra}\n\n"
                f"Pásame la URL completa, por ej. `github.com/{org}/{repos[0]}`."
            )

        await self._guardar_turno(role="assistant", content=texto)
        await audit.append(
            self._pool,
            actor="for3s",
            action="message_out",
            detail={"session": self._session_id, "modo": "org", "org": org, "n_repos": len(repos)},
        )
        return LLMResponse(text=texto, input_tokens=0, output_tokens=0, model=provider._model)
