#!/usr/bin/env python3
"""Renderiza una SPA con Chromium headless y devuelve su texto como JSON.

Uso: python3 render.py <url>
Salida (stdout): {"ok": bool, "titulo": str, "texto": str, "error": str|None}

Corre DENTRO del contenedor Docker oficial de Playwright (que trae Chromium +
las libs del sistema). El bot de For3s lo invoca con 'docker run' pasándole la
URL; así el render de JS vive aislado y no depende del Ubuntu 26.04 del host.
"""

import json
import sys

from playwright.sync_api import sync_playwright

MAX_TEXTO = 12000


def render(url: str) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            )
            # domcontentloaded + un respiro para que la SPA pinte su contenido.
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


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "titulo": "", "texto": "", "error": "falta url"}))
        return
    try:
        print(json.dumps(render(sys.argv[1]), ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — reportamos cualquier fallo como JSON
        print(
            json.dumps(
                {"ok": False, "titulo": "", "texto": "", "error": f"{type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
