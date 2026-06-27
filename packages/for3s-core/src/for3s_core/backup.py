"""For3s OS — Backup automático de la BD (H6 "SE CUIDA", Sub-paso 11).

Backup 3-2-1 FOUNDATION (single-user, esta fase):
  · 3 copias: el original + los N backups rotados
  · 2 medios: BD en disco + dumps en carpeta de backups
  · 1 off-site: ⏳ PENDIENTE (decisión de diseño: local+rotación ahora; off-site a otra
    máquina/bucket cuando se defina el destino). Ver PENDIENTES "H6-backup-offsite".

El WAL + PITR + disaster recovery completo es H16. Esto es el foundation: pg_dump
periódico + verificación + rotación, suficiente para confiar en el olvido nocturno
de la Microglía (que empieza a borrar sin supervisión cuando se active el modo real).

Lo dispara el job_backup nocturno (tasks.py) antes que CLS. Defensivo.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("for3s.backup")

# Cargar el .env al importar (igual que tasks.py): este módulo lee vars FOR3S_*
# a nivel de módulo; sin esto, FOR3S_BACKUP_OFFSITE quedaría en su default vacío.
from for3s_core.config import _load_dotenv  # noqa: E402

_load_dotenv(Path.cwd() / ".env")

# Carpeta de backups (la misma que usamos a mano en H5/H6).
BACKUP_DIR = Path(os.environ.get("FOR3S_BACKUP_DIR", str(Path.home() / "for3s-backups")))
PREFIJO = "auto_for3s_"            # nombre de los backups automáticos
RETENER = int(os.environ.get("FOR3S_BACKUP_RETENER", "14"))  # cuántos conservar
TAMANO_MINIMO_BYTES = 10_000       # un dump válido pesa más que esto (anti-truncado)

# OFF-SITE (el "1" del 3-2-1): copia cada backup a OTRA máquina fuera del server.
# Destino vía SSH (rsync). Configurable por env. Vacío = off-site desactivado.
# Formato destino: usuario@host:/ruta  (ej. user@10.0.0.5:~/for3s-backups-offsite)
OFFSITE_DESTINO = os.environ.get("FOR3S_BACKUP_OFFSITE", "").strip()
OFFSITE_SSH_KEY = os.environ.get("FOR3S_BACKUP_SSH_KEY", str(Path.home() / ".ssh/id_ed25519"))


def _parse_dsn(database_url: str) -> dict:
    """Extrae host/puerto/db/usuario/password del DSN (postgresql+asyncpg://...).
    pg_dump no entiende '+asyncpg' ni la password en la URL → la sacamos aparte."""
    m = re.match(
        r"postgresql(?:\+\w+)?://([^:]+):([^@]+)@([^:/]+):?(\d+)?/(\w+)",
        database_url,
    )
    if not m:
        raise ValueError("DSN no reconocido para backup")
    user, pw, host, port, db = m.groups()
    return {"user": user, "pw": pw, "host": host, "port": port or "5432", "db": db}


def hacer_backup(database_url: str) -> Path:
    """Ejecuta pg_dump → archivo con timestamp. Verifica que no esté truncado.
    Devuelve la ruta. Lanza si falla (el caller decide qué hacer)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    info = _parse_dsn(database_url)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    destino = BACKUP_DIR / f"{PREFIJO}{stamp}.sql"

    env = dict(os.environ, PGPASSWORD=info["pw"])
    cmd = [
        "pg_dump", "-U", info["user"], "-h", info["host"], "-p", info["port"],
        "-d", info["db"], "--no-owner", "--no-privileges", "-f", str(destino),
    ]
    subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=300)

    # verificación anti-truncado
    size = destino.stat().st_size if destino.exists() else 0
    if size < TAMANO_MINIMO_BYTES:
        raise RuntimeError(f"backup sospechosamente pequeño ({size} bytes): {destino}")
    logger.info("[backup] creado %s (%d bytes)", destino.name, size)
    return destino


def rotar(retener: int = RETENER) -> int:
    """Borra los backups automáticos más viejos, conservando los `retener` más
    recientes. NO toca los backups manuales (pre_h5_*, pre_h6_*). Devuelve cuántos borró."""
    autos = sorted(BACKUP_DIR.glob(f"{PREFIJO}*.sql"))
    sobran = autos[:-retener] if len(autos) > retener else []
    for p in sobran:
        try:
            p.unlink()
            logger.info("[backup] rotado (borrado viejo) %s", p.name)
        except OSError as e:
            logger.warning("[backup] no se pudo rotar %s: %s", p.name, e)
    return len(sobran)


def copiar_offsite(ruta: Path) -> bool:
    """Copia un backup a la máquina OFF-SITE (el '1' del 3-2-1) vía rsync+SSH.
    DEFENSIVA: si el destino está apagado/inalcanzable, NO rompe el backup local —
    loguea el aviso y devuelve False (esa noche se salta; la siguiente reintenta).
    Desactivada si FOR3S_BACKUP_OFFSITE está vacío."""
    if not OFFSITE_DESTINO:
        return False
    try:
        ssh_cmd = (
            f"ssh -i {OFFSITE_SSH_KEY} -o BatchMode=yes "
            f"-o StrictHostKeyChecking=no -o ConnectTimeout=15"
        )
        subprocess.run(
            ["rsync", "-az", "-e", ssh_cmd, str(ruta), OFFSITE_DESTINO],
            check=True, capture_output=True, timeout=300,
        )
        logger.info("[backup] off-site OK → %s (%s)", OFFSITE_DESTINO, ruta.name)
        return True
    except Exception as e:  # noqa: BLE001 — off-site nunca rompe el backup local
        logger.warning(
            "[backup] off-site FALLÓ (no crítico, el local sí se hizo): %s", type(e).__name__,
        )
        return False


def backup_y_rotar(database_url: str, retener: int = RETENER) -> tuple[Path, int]:
    """Hace un backup verificado, lo copia off-site (si está configurado) y rota
    los viejos. Devuelve (ruta, n_borrados)."""
    ruta = hacer_backup(database_url)
    copiar_offsite(ruta)  # defensiva: si falla, no rompe nada
    borrados = rotar(retener)
    return ruta, borrados
