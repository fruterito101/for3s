"""Tests de H4 (sin red): KEK crypto + sandbox de lint.

NOTA (2026-06-18): se quitaron los tests del GitHub artesanal (github_tool /
pr_review), que fue BORRADO al migrar a GitHub MCP. Quedan los tests vigentes:
cifrado KEK por workspace y el sandbox de lint en contenedor. La lógica nueva
(MCP, subbloques, modos, ficha, etc.) se cubre en test_pulido_mvp.py.
"""

from __future__ import annotations

from for3s_core import crypto

# ---- KEK / crypto ----


def test_kek_roundtrip(tmp_path) -> None:
    master = crypto.load_or_create_master_key(tmp_path / "m.key")
    wkey = crypto.derive_workspace_key(master, "brian")
    nonce, ct = crypto.encrypt(wkey, "gho_secreto_123")
    assert crypto.decrypt(wkey, nonce, ct) == "gho_secreto_123"


def test_kek_no_cruza_workspaces(tmp_path) -> None:
    master = crypto.load_or_create_master_key(tmp_path / "m.key")
    k1 = crypto.derive_workspace_key(master, "brian")
    k2 = crypto.derive_workspace_key(master, "otro")
    nonce, ct = crypto.encrypt(k1, "secreto")
    # la clave de otro workspace NO puede descifrar (aislamiento)
    import pytest
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        crypto.decrypt(k2, nonce, ct)


def test_master_key_se_persiste(tmp_path) -> None:
    p = tmp_path / "m.key"
    k1 = crypto.load_or_create_master_key(p)
    k2 = crypto.load_or_create_master_key(p)  # 2ª vez la lee, no genera otra
    assert k1 == k2


# ---- sandbox (lint en contenedor) ----


def test_sandbox_sin_archivos_py_devuelve_vacio() -> None:
    from for3s_core import sandbox

    assert sandbox.lint_archivos({"README.md": "# hola"}) == ""


def test_sandbox_archivos_vacios_devuelve_vacio() -> None:
    from for3s_core import sandbox

    assert sandbox.lint_archivos({}) == ""
