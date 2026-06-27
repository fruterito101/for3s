# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""Fetch de URLs públicas no-GitHub (anotación #1 de el dueño, 2026-06-15).

For3s era cuadrado: ante una URL no-GitHub (Lu.ma, blogs, docs) decía "solo
accedo a GitHub, ábrelo tú". Pero es un segundo cerebro versátil — debe poder
LEER cualquier página pública y responder sobre su contenido.

Dos capas (2026-06-18, cierre del pendiente 'límites web fetch JS/SPA'):
  1. httpx directo: rápido, sirve para páginas con HTML servidor (blogs, docs).
  2. Si el HTML viene casi vacío (una SPA que pinta todo con JS, como antes
     pasaba con donutbrowser.com), caemos al contenedor Docker 'for3s-render'
     que lanza Chromium headless, ejecuta el JS y devuelve el texto YA pintado.
     El contenedor sortea que el host corre Ubuntu 26.04 (Playwright no tiene
     build nativo para esa versión todavía).

Login: NO peleamos contra muros de sesión / anti-bot. Si la página exige
iniciar sesión, For3s lo dice honestamente en vez de fingir que la leyó.

Read-only, URLs públicas, con límites de seguridad.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx

# Límites de seguridad
TIMEOUT = 20.0
MAX_BYTES = 600_000  # no descargar páginas gigantes
MAX_TEXTO = 12_000  # lo que se le pasa a Claude (acotado)
_UA = "Mozilla/5.0 (compatible; For3sOS/1.0)"

# Umbral: si el texto útil extraído por httpx es más corto que esto, asumimos
# que es una SPA sin renderizar (cáscara JS) y vale la pena el render headless.
UMBRAL_SPA = 350
RENDER_IMAGE = "for3s-render:latest"
RENDER_TIMEOUT = 45.0  # docker run + render de Chromium

# Señales de que la página exige iniciar sesión (no peleamos contra esto).
_SENALES_LOGIN = (
    "sign in",
    "log in",
    "login",
    "iniciar sesión",
    "inicia sesión",
    "create an account",
    "you must be logged in",
    "please enable javascript",
)

# Señales de un MURO ANTI-BOT (la página detectó acceso automatizado y nos da
# una pantalla de "no eres humano" en vez del contenido). Igual que con login,
# NO peleamos contra esto (Amazon, Cloudflare, etc.): avisamos con honestidad.
_SENALES_ANTIBOT = (
    "continue shopping",  # Amazon: "Click the button below to continue shopping"
    "to discuss automated access",  # Amazon bot wall
    "are you a human",
    "are you a robot",
    "robot check",
    "captcha",
    "verifying you are human",
    "checking your browser",  # Cloudflare
    "access denied",
    "request blocked",
    "unusual traffic",
    "enable cookies",
    "verify you are human",
)


def _html_a_texto(html_raw: str) -> str:
    """Extrae texto legible de un HTML (sin parser pesado): quita script/style,
    tags, colapsa espacios. Suficiente para que Claude entienda la página."""
    # quitar bloques script/style enteros
    html_raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html_raw)
    # title y meta description (señales fuertes)
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_raw)
    if m:
        title = m.group(1).strip()
    desc = ""
    _re_desc = r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']'
    m = re.search(_re_desc, html_raw)
    if m:
        desc = m.group(1).strip()
    # og:title / og:description (muchas SPAs como Lu.ma viven aquí)
    og = []
    for prop in ("og:title", "og:description", "og:site_name"):
        _re_og = rf'(?is)<meta[^>]+property=["\']{prop}["\'][^>]+content=["\'](.*?)["\']'
        m = re.search(_re_og, html_raw)
        if m:
            og.append(m.group(1).strip())
    # quitar todos los tags restantes
    texto = re.sub(r"(?s)<[^>]+>", " ", html_raw)
    # decodificar entidades básicas + colapsar espacios
    texto = re.sub(r"&nbsp;?", " ", texto)
    texto = re.sub(r"&amp;", "&", texto)
    texto = re.sub(r"&lt;", "<", texto)
    texto = re.sub(r"&gt;", ">", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    cabecera = ""
    if title:
        cabecera += f"TÍTULO: {title}\n"
    if desc:
        cabecera += f"DESCRIPCIÓN: {desc}\n"
    if og:
        cabecera += "META: " + " · ".join(og) + "\n"
    return (cabecera + "\nCONTENIDO:\n" + texto)[:MAX_TEXTO]


def _largo_contenido(texto: str) -> int:
    """Largo del CONTENIDO real (sin la cabecera TÍTULO/DESC/META), para decidir
    si la página venía vacía (SPA). Si no hay marcador, mide todo."""
    marca = "\nCONTENIDO:\n"
    i = texto.find(marca)
    cuerpo = texto[i + len(marca) :] if i >= 0 else texto
    return len(cuerpo.strip())


async def _render_headless(url: str) -> tuple[bool, str]:
    """Renderiza la URL en el contenedor Docker (Chromium headless). Devuelve
    (ok, texto_o_error). Usa --network host para alcanzar internet del server."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            RENDER_IMAGE,
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT)
    except TimeoutError:
        return False, "render: el navegador tardó demasiado"
    except Exception as exc:  # noqa: BLE001
        return False, f"render no disponible ({type(exc).__name__}: {exc})"
    if not out:
        return False, f"render sin salida ({(err or b'').decode(errors='replace')[:200]})"
    try:
        data = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, "render: salida ilegible"
    if not data.get("ok"):
        return False, f"render falló ({data.get('error')})"
    titulo = (data.get("titulo") or "").strip()
    cuerpo = (data.get("texto") or "").strip()
    if not cuerpo:
        return False, "vacío"
    cabecera = f"TÍTULO: {titulo}\n" if titulo else ""
    return True, (cabecera + "\nCONTENIDO:\n" + cuerpo)[:MAX_TEXTO]


def _normaliza(u: str) -> str:
    """URL normalizada para comparar origen vs destino: sin esquema, sin barra
    final, en minúsculas. Así 'http://x.com/' y 'https://X.com' se ven iguales
    y no marcamos un 'redirect' que en realidad es el mismo sitio."""
    u = re.sub(r"^https?://", "", u.strip().lower())
    return u.rstrip("/")


def _huele_a_login(texto: str) -> bool:
    """True si lo poco que se leyó parece un muro de sesión."""
    bajo = texto.lower()
    return any(s in bajo for s in _SENALES_LOGIN)


def _huele_a_antibot(texto: str) -> bool:
    """True si la página nos dio una pantalla anti-bot en vez del contenido."""
    bajo = texto.lower()
    return any(s in bajo for s in _SENALES_ANTIBOT)


async def fetch_url(url: str) -> tuple[bool, str]:
    """Trae una URL pública y devuelve (ok, texto_legible_o_error).

    Capa 1 httpx; si la página es una SPA (poco texto), capa 2 render headless.
    Si exige login, lo dice honestamente."""
    if not re.match(r"https?://", url):
        url = "https://" + url
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}
    raw = ""
    httpx_ok = False
    # URL FINAL tras seguir la cadena de redirects (links cortos a.co/, bit.ly,
    # amzn.to…). httpx ya los sigue con follow_redirects; aquí la CAPTURAMOS para
    # exponerla — así For3s sabe (y le dice al usuario) a dónde llevaba el link.
    url_final = url
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as cli:
            resp = await cli.get(url, headers=headers)
        url_final = str(resp.url)
        if resp.status_code < 400:
            ctype = resp.headers.get("content-type", "")
            if "text" in ctype or "html" in ctype or "json" in ctype:
                raw = resp.text[:MAX_BYTES]
                httpx_ok = True
    except Exception:  # noqa: BLE001 — la red doméstica parpadea; intentamos render igual
        pass

    # Si el link se acortó/redirigió a otro lado, lo anunciamos como primera línea
    # del contenido (transparencia: "ese a.co/… lleva a tal producto de Amazon").
    redirigio = _normaliza(url_final) != _normaliza(url)
    prefijo_final = f"ENLACE FINAL: {url_final}\n" if redirigio else ""

    texto = _html_a_texto(raw) if raw else ""

    # ¿Contenido suficiente? Si sí, lo devolvemos directo (rápido).
    if texto and _largo_contenido(texto) >= UMBRAL_SPA:
        return True, prefijo_final + texto

    # Contenido pobre o nulo → puede ser una SPA. Render headless.
    ok_r, texto_r = await _render_headless(url)
    if ok_r and _largo_contenido(texto_r) >= 50 and not _huele_a_antibot(texto_r):
        return True, prefijo_final + texto_r

    # ¿Muro ANTI-BOT? (Amazon, Cloudflare…). NO peleamos: avisamos honesto y, lo
    # útil, DECIMOS a dónde llevaba el link aunque no pudiéramos leer el contenido.
    if _huele_a_antibot(texto) or _huele_a_antibot(texto_r):
        destino = f" El enlace lleva a: {url_final}." if redirigio or url_final else ""
        return False, (
            "Esa página bloquea accesos automáticos (anti-bot, típico de Amazon o "
            f"sitios con protección).{destino} No puedo leer su contenido sin ser un "
            "navegador 'humano'. Si quieres, pégame el título/precio o lo que necesites."
        )

    # ¿Muro de LOGIN?
    if _huele_a_login(texto) or _huele_a_login(texto_r):
        destino = f" El enlace lleva a: {url_final}." if redirigio else ""
        return False, (
            f"Esa página requiere iniciar sesión.{destino} No puedo entrar a contenido "
            "tras un login. Si puedes, pégame el texto o pásame un enlace público."
        )

    # httpx trajo algo (aunque corto) y el render no mejoró → devolvemos lo poco.
    if texto and httpx_ok:
        return True, prefijo_final + texto

    # Nada legible. Si al menos resolvimos a dónde apuntaba, lo decimos (útil).
    destino = f" (El enlace apunta a: {url_final}.)" if redirigio else ""
    return False, (
        "No pude leer contenido de esa URL (puede requerir login, estar caída, "
        f"o cargar todo con JavaScript de una forma que no pude renderizar).{destino}"
    )
