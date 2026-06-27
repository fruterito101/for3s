"""Hora LOCAL del usuario (2026-06-18).

PROBLEMA: el servidor corre en UTC (red de casa), pero el usuario puede estar
en otra zona. Telegram NO manda la zona horaria — solo UTC y, a veces, el
language_code del teléfono (es-MX, en-US...). Aquí deducimos la zona del
usuario de ese código y damos la fecha/hora LOCAL para inyectarla al prompt,
así For3s nunca se confunde con la hora del servidor.

Si el código no trae país (solo 'es'/'en') o no viene → default CDMX.
Extensible a un /zona por usuario en el futuro.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

ZONA_DEFAULT = "America/Mexico_City"  # zona por defecto

# Mapa language_code (con país) → zona IANA. Solo los que traen país son fiables.
_LANG_A_ZONA = {
    "es-mx": "America/Mexico_City",
    "es-ar": "America/Argentina/Buenos_Aires",
    "es-co": "America/Bogota",
    "es-cl": "America/Santiago",
    "es-pe": "America/Lima",
    "es-es": "Europe/Madrid",
    "es-us": "America/Mexico_City",  # hispano en US, asumir CDMX salvo /zona
    "en-us": "America/New_York",
    "en-gb": "Europe/London",
    "pt-br": "America/Sao_Paulo",
}


def zona_de_language(language_code: str | None) -> str:
    """Deduce la zona IANA del language_code de Telegram. Default CDMX si el
    código no trae país o es desconocido (evita errores de 'es'=España vs MX)."""
    if not language_code:
        return ZONA_DEFAULT
    code = language_code.strip().lower()
    if code in _LANG_A_ZONA:
        return _LANG_A_ZONA[code]
    return ZONA_DEFAULT  # 'es'/'en' sueltos o desconocidos → CDMX


def ahora_local(zona: str | None = None) -> datetime:
    """datetime actual en la zona del usuario (no la del servidor)."""
    z = zona or ZONA_DEFAULT
    try:
        return datetime.now(UTC).astimezone(ZoneInfo(z))
    except Exception:
        return datetime.now(UTC).astimezone(ZoneInfo(ZONA_DEFAULT))


_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES = ("", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def contexto_temporal(language_code: str | None = None, zona: str | None = None) -> str:
    """Frase con la fecha/hora LOCAL del usuario, para inyectar al prompt de
    Claude. Ej: 'Fecha y hora actual del usuario: jueves 18 de junio de 2026,
    9:15 p. m. (zona America/Mexico_City).'"""
    z = zona or zona_de_language(language_code)
    n = ahora_local(z)
    dia = _DIAS[n.weekday()]
    h12 = n.strftime("%I:%M %p").lstrip("0").lower().replace("am", "a. m.").replace("pm", "p. m.")
    return (f"Fecha y hora actual DEL USUARIO (no del servidor): {dia} "
            f"{n.day} de {_MESES[n.month]} de {n.year}, {h12} (zona {z}).")
