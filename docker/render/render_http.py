#!/usr/bin/env python3
"""Servidor HTTP del renderizador de SPAs de For3s (HERMANO de red, v1.1 BUG-9b).

ANTES: el bot hacía `docker run for3s-render <url>` (un disparo). Pero el bot vive
en un contenedor sin Docker → fallaba (BUG-9). AHORA: este script corre como un
SERVICIO HERMANO permanente; el bot le pide por HTTP:

    GET http://render:8080/?url=<URL>
    → {"ok": bool, "titulo": str, "texto": str, "error": str|None}

Corre DENTRO de la imagen oficial de Playwright (Chromium + libs). Reusa la misma
lógica de render que el render.py original. stdlib only (sin deps extra).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

MAX_TEXTO = 12000
PORT = int(os.environ.get("RENDER_PORT", "8080"))


def render(url: str) -> dict:
    """Renderiza una URL con Chromium headless y devuelve {ok,titulo,texto,error}.
    Misma lógica que el render.py original (sync, un browser por request)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            )
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # algunas SPAs nunca quedan 'idle'; seguimos con lo que haya
            titulo = (page.title() or "").strip()
            texto = page.evaluate('() => document.body ? document.body.innerText : ""')
            return {
                "ok": True,
                "titulo": titulo,
                "texto": (texto or "").strip()[:MAX_TEXTO],
                "error": None,
            }
        finally:
            browser.close()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        # healthcheck simple: GET /health → 200 (para PR2 monitoreo por HTTP)
        if parsed.path == "/health":
            self._json(200, {"ok": True, "service": "render"})
            return
        qs = parse_qs(parsed.query)
        url = (qs.get("url") or [""])[0]
        if not url:
            self._json(400, {"ok": False, "titulo": "", "texto": "", "error": "falta url"})
            return
        try:
            self._json(200, render(url))
        except Exception as e:  # noqa: BLE001 — cualquier fallo se reporta como JSON
            self._json(
                200,
                {"ok": False, "titulo": "", "texto": "", "error": f"{type(e).__name__}: {e}"},
            )

    def log_message(self, *args) -> None:  # noqa: D401 — silenciar el log ruidoso por request
        pass


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[render] HTTP server escuchando en :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
