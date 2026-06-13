"""Tests de H4 (sin red): KEK crypto + parser de URL de PR + contexto."""

from __future__ import annotations

from for3s_core import crypto
from for3s_core.github_tool import (
    PRFile,
    PullRequest,
    parse_pr_url,
    pr_to_context,
)

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


# ---- parser de URL de PR ----


def test_parse_pr_url_valido() -> None:
    assert parse_pr_url("analiza https://github.com/fruteroclub/intern-os/pull/19") == (
        "fruteroclub",
        "intern-os",
        19,
    )


def test_parse_pr_url_sin_url() -> None:
    assert parse_pr_url("hola, cómo estás?") is None


def test_parse_pr_url_ignora_issues() -> None:
    # /issues/ NO es /pull/ → no debe matchear
    assert parse_pr_url("https://github.com/x/y/issues/5") is None


# ---- contexto QA ----


def test_pr_to_context_incluye_metadata_y_diff() -> None:
    pr = PullRequest(
        owner="x",
        repo="y",
        number=1,
        title="Arreglo bug",
        body="descripción",
        author="brian",
        state="open",
        base="main",
        head="fix",
        additions=10,
        deletions=2,
        changed_files=1,
        files=[PRFile("a.py", "modified", 10, 2, "+codigo nuevo")],
    )
    ctx = pr_to_context(pr)
    assert "Arreglo bug" in ctx
    assert "a.py" in ctx
    assert "+codigo nuevo" in ctx


def test_pr_to_context_avisa_omitidos() -> None:
    pr = PullRequest(
        owner="x",
        repo="y",
        number=1,
        title="t",
        body="",
        author="a",
        state="open",
        base="main",
        head="f",
        additions=0,
        deletions=0,
        changed_files=100,
        omitted_files=70,
    )
    assert "OMITIDOS" in pr_to_context(pr)


# ---- sandbox (lint en contenedor) ----


def test_sandbox_sin_archivos_py_devuelve_vacio() -> None:
    from for3s_core import sandbox

    assert sandbox.lint_archivos({"README.md": "# hola"}) == ""


def test_sandbox_archivos_vacios_devuelve_vacio() -> None:
    from for3s_core import sandbox

    assert sandbox.lint_archivos({}) == ""


def test_patch_to_source_extrae_lineas_anadidas() -> None:
    from for3s_core.github_tool import PRFile

    f = PRFile(
        filename="x.py",
        status="modified",
        additions=1,
        deletions=1,
        patch="@@ -1,2 +1,2 @@\n contexto\n-vieja\n+nueva linea\n",
    )
    src = f.patch_to_source()
    assert "nueva linea" in src
    assert "vieja" not in src


# ---- detección multi-tipo (PR / gist / blob) ----


def test_detect_pr() -> None:
    from for3s_core.github_tool import detect_resource

    t, d = detect_resource("mira https://github.com/o/r/pull/42")
    assert t == "pr" and d == ("o", "r", 42)


def test_detect_gist() -> None:
    from for3s_core.github_tool import detect_resource

    t, d = detect_resource("https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f")
    assert t == "gist" and d == ("442a6bf555914893e9891c11519de94f",)


def test_detect_blob() -> None:
    from for3s_core.github_tool import detect_resource

    t, d = detect_resource("https://github.com/o/r/blob/main/src/app.py")
    assert t == "blob" and d == ("o", "r", "main", "src/app.py")


def test_detect_none() -> None:
    from for3s_core.github_tool import detect_resource

    t, d = detect_resource("hola cómo estás")
    assert t == "none"


def test_snippet_to_context() -> None:
    from for3s_core.github_tool import CodeSnippet, snippet_to_context

    s = CodeSnippet(source="Gist de x", files={"a.py": "print(1)"})
    ctx = snippet_to_context(s)
    assert "Gist de x" in ctx and "print(1)" in ctx and "a.py" in ctx
