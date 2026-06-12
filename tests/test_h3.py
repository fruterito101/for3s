"""Tests de H3 (canal Telegram) — lógica pura, sin red ni Telegram real."""

from __future__ import annotations

from for3s_core.telegram_channel import MAX_MESSAGE_LENGTH, OwnerStore, split_message


def test_split_texto_corto_un_chunk() -> None:
    assert split_message("hola") == ["hola"]


def test_split_texto_vacio() -> None:
    assert split_message("") == []


def test_split_respeta_limite_y_conserva_contenido() -> None:
    text = "palabra " * 2000  # ~16,000 chars
    chunks = split_message(text)
    assert len(chunks) >= 3
    assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)
    # el contenido se conserva (módulo espacios de corte)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_split_prefiere_cortar_en_parrafos() -> None:
    text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    chunks = split_message(text)
    assert chunks[0] == "a" * 3000
    assert chunks[1] == "b" * 3000


def test_owner_fail_closed_sin_dueno(tmp_path) -> None:
    store = OwnerStore(tmp_path / "owner.json")
    assert store.get_owner() is None
    assert store.is_authorized(12345) is False  # sin dueño → NADIE pasa
    assert store.is_authorized(None) is False


def test_owner_primer_start_registra(tmp_path) -> None:
    store = OwnerStore(tmp_path / "owner.json")
    store.set_owner(777)
    assert store.get_owner() == 777
    assert store.is_authorized(777) is True


def test_owner_rechaza_a_otros(tmp_path) -> None:
    store = OwnerStore(tmp_path / "owner.json")
    store.set_owner(777)
    assert store.is_authorized(999) is False
    assert store.is_authorized(None) is False


def test_cupo_normal_muestra_porcentaje() -> None:
    from for3s_core.telegram_channel import format_cupo

    out = format_cupo(0.23, 0.10)
    assert "23%" in out and "🔋" in out


def test_cupo_alerta_al_80() -> None:
    from for3s_core.telegram_channel import format_cupo

    out = format_cupo(0.82, 0.50)
    assert "⚠️" in out and "82%" in out and "18%" in out


def test_cupo_sin_dato_vacio() -> None:
    from for3s_core.telegram_channel import format_cupo

    assert format_cupo(None, None) == ""


def test_md_to_telegram_quita_negritas() -> None:
    from for3s_core.telegram_channel import md_to_telegram

    assert md_to_telegram("Soy **For3s OS**") == "Soy For3s OS"


def test_md_to_telegram_quita_cursiva_y_code() -> None:
    from for3s_core.telegram_channel import md_to_telegram

    assert md_to_telegram("usa *esto* y `codigo`") == "usa esto y codigo"


def test_md_to_telegram_texto_normal_intacto() -> None:
    from for3s_core.telegram_channel import md_to_telegram

    assert md_to_telegram("hola mundo, 2*3=6") == "hola mundo, 2*3=6"
