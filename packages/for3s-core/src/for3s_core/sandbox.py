# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""Sandbox de análisis de For3s OS (H4) — lint del código del PR en contenedor.

Análisis OBJETIVO complementario al de Claude: extrae los archivos .py del PR,
los escribe en un dir temporal y corre `ruff` DENTRO de un contenedor Docker
ENDURECIDO (sin red, sin root, read-only, límites de CPU/mem). Los hallazgos
se suman al reporte QA.

Aislamiento (Grafo §6, R4 container hardening):
  --network none      sin acceso a red
  --read-only         filesystem inmutable
  --user 10001        sin privilegios
  --memory/--cpus     límites de recursos
  --pids-limit        anti fork-bomb
  --rm                se destruye al terminar

Si Docker o la imagen no están disponibles, devuelve "" (degrada con gracia:
el análisis de Claude sigue funcionando sin el lint).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

IMAGE = "for3s-workspace:latest"
_TIMEOUT = 30  # segundos máx para el lint (anti-cuelgue)
_MAX_FINDINGS_CHARS = 4000


def docker_disponible() -> bool:
    return shutil.which("docker") is not None


def imagen_existe() -> bool:
    if not docker_disponible():
        return False
    r = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True,
        timeout=15,
    )
    return r.returncode == 0


def lint_archivos(archivos: dict[str, str]) -> str:
    """Corre ruff sobre {ruta: contenido} en un contenedor aislado.

    Devuelve los hallazgos (texto) o "" si no hay Docker/imagen o no hay .py.
    NUNCA lanza: si algo falla, degrada a "" (el reporte QA sigue sin lint).
    """
    pys = {name: body for name, body in archivos.items() if name.endswith(".py") and body}
    if not pys or not imagen_existe():
        return ""

    with tempfile.TemporaryDirectory(prefix="for3s-lint-") as tmp:
        base = Path(tmp)
        # el contenedor corre como uid 10001: el dir y los archivos deben ser
        # legibles por "otros" (o-rx), si no ruff los ve como vacíos/inexistentes.
        base.chmod(0o755)
        for name, body in pys.items():
            # aplanar la ruta para evitar escapes (../) — solo el nombre de archivo
            safe = Path(name).name
            fp = base / safe
            fp.write_text(body, encoding="utf-8")
            fp.chmod(0o644)
        try:
            r = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "10001",
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "128",
                    "-v",
                    f"{base}:/work:ro",
                    IMAGE,
                    # --no-cache: /work es read-only, ruff no puede escribir caché
                    "check",
                    "/work",
                    "--no-cache",
                    "--output-format",
                    "concise",
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        out = (r.stdout or "").strip()
        if not out or "All checks passed" in out:
            return ""
        return out[:_MAX_FINDINGS_CHARS]
