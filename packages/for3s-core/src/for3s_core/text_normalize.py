"""Normalización de texto de entrada de For3s OS.

Capa estándar para que los DETECTORES/COMPARADORES (huele_a_github,
detect_short_ref, comandos futuros, etc.) trabajen sobre texto limpio, sin
importar cómo escriba el usuario: MAYÚSCULAS, minúsculas, InTeRcAlAdO o con
acentos. Así un mismo término se reconoce siempre igual.

IMPORTANTE: esto es SOLO para detección/comparación. El texto ORIGINAL del
usuario se conserva intacto para mandárselo a Claude y guardarlo en memoria
(Claude entiende cualquier capitalización, y las mayúsculas/acentos/nombres
propios del usuario son parte de su mensaje — no se destruyen).
"""

from __future__ import annotations

import re
import unicodedata

_ESPACIOS_RE = re.compile(r"\s+")


def normalizar(texto: str) -> str:
    """Estandariza texto para detección: minúsculas + sin acentos + espacios.

    Ejemplos:
      "CUANTOS ISSUES"   → "cuantos issues"
      "Análisis"         → "analisis"
      "  el   PR  134 "  → "el pr 134"
    """
    if not texto:
        return ""
    # minúsculas
    t = texto.lower()
    # quitar acentos/diacríticos (NFD descompone, se filtran las marcas)
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    # colapsar espacios múltiples + recortar
    t = _ESPACIOS_RE.sub(" ", t).strip()
    return t
