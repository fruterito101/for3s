"""KEK foundation de For3s OS (H4) — jerarquía de cifrado de secretos (R4 v1).

Diseño (R4 B1, Grafo §6 capa 1):
  MASTER KEY (archivo local, chmod 600, fuera del repo)
      └─ HKDF-SHA256 por workspace → WORKSPACE KEY
              └─ AES-256-GCM → secretos cifrados (en BD)

Principios:
  • Los secretos NUNCA viven en texto plano en BD ni en el repo.
  • "Decrypt minimum": el plaintext existe solo el instante en que se usa.
  • v1: master key en disco local protegido (~/.for3s/master.key). El paso
    a TPM/USB offline llega en H16 (R10) — esta API no cambia.
"""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # GCM estándar


def load_or_create_master_key(path: Path) -> bytes:
    """Carga la master key; si no existe, la genera (32 bytes aleatorios, 600)."""
    if path.exists():
        key = path.read_bytes()
        if len(key) != _KEY_BYTES:
            raise RuntimeError(f"master key corrupta en {path} (len={len(key)})")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _secrets.token_bytes(_KEY_BYTES)
    # escribir con permisos restrictivos desde el nacimiento
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


def derive_workspace_key(master: bytes, workspace_id: str) -> bytes:
    """Deriva la clave del workspace con HKDF-SHA256 (R4)."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=b"for3s-os-v1",
        info=f"workspace:{workspace_id}".encode(),
    )
    return hkdf.derive(master)


def encrypt(workspace_key: bytes, plaintext: str) -> tuple[bytes, bytes]:
    """Cifra con AES-256-GCM. Devuelve (nonce, ciphertext+tag)."""
    nonce = _secrets.token_bytes(_NONCE_BYTES)
    ct = AESGCM(workspace_key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce, ct


def decrypt(workspace_key: bytes, nonce: bytes, ciphertext: bytes) -> str:
    """Descifra. Lanza si el dato fue alterado (GCM autentica)."""
    pt = AESGCM(workspace_key).decrypt(nonce, ciphertext, None)
    return pt.decode("utf-8")
