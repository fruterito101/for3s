"""Conversor Markdown (de Claude) → HTML de Telegram.

Telegram Bot API soporta parse_mode=HTML con un set ACOTADO de tags:
<b> <i> <u> <s> <code> <pre> <a>. NO soporta tablas, headers, listas como
tags → esos se renderizan como texto. La clave: escapar < > & SIEMPRE, y
proteger los bloques de código para que su contenido no se interprete.

Por qué HTML y no MarkdownV2: MarkdownV2 obliga a escapar 18 caracteres
(_*[]()~`>#+-=|{}.!) y un solo desbalance rompe el mensaje ENTERO. HTML solo
necesita escapar 3 (< > &) y es mucho más tolerante. Para texto arbitrario de
un LLM, HTML es la opción robusta (confirmado con doc de Telegram + PTB).
"""

from __future__ import annotations

import html
import re


def md_a_html_telegram(texto: str) -> str:
    """Convierte markdown de Claude a HTML válido de Telegram.

    Maneja: bloques ```code```, `code` inline, **negrita**, *cursiva*,
    # headers (→ negrita), tablas markdown (→ monoespaciado), enlaces.
    Escapa < > & en todo el texto que no sea código ya protegido.
    """
    # 0) Claude a veces escribe <pre>/<code> LITERALES en su texto. Capturarlos
    #    como bloques de código (su contenido se escapará al restaurar) para que
    #    NO se vean crudos (bug 2026-06-15: aparecía <pre>| Zona |... en pantalla).
    bloques: list[str] = []

    def _guardar_pre_literal(m: re.Match) -> str:
        bloques.append(m.group(1))
        return f"\x00BLOQUE{len(bloques) - 1}\x00"

    texto = re.sub(r"(?is)<pre>(.*?)</pre>", _guardar_pre_literal, texto)

    # 1) PROTEGER bloques de código ```...``` (guardar y reemplazar por token)

    def _guardar_bloque(m: re.Match) -> str:
        contenido = m.group(2)  # sin los ``` ni el lenguaje
        bloques.append(contenido)
        return f"\x00BLOQUE{len(bloques) - 1}\x00"

    # ```lang\n...\n``` o ```...```
    texto = re.sub(r"```(\w*)\n?(.*?)```", _guardar_bloque, texto, flags=re.DOTALL)

    # 1.5) PROTEGER tablas markdown (filas con |): a <pre> para que se alineen
    #      en monoespaciado (Telegram no renderiza tablas HTML).
    def _guardar_tabla(m: re.Match) -> str:
        bloques.append(m.group(0).strip())
        return f"\x00BLOQUE{len(bloques) - 1}\x00"

    # 2+ líneas consecutivas que empiezan (con espacios opcionales) por |
    texto = re.sub(
        r"(?:^[ \t]*\|.*\|[ \t]*$\n?){2,}",
        _guardar_tabla,
        texto,
        flags=re.MULTILINE,
    )

    # 2) PROTEGER código inline `...`
    inline: list[str] = []

    def _guardar_inline(m: re.Match) -> str:
        inline.append(m.group(1))
        return f"\x00INLINE{len(inline) - 1}\x00"

    texto = re.sub(r"`([^`\n]+?)`", _guardar_inline, texto)

    # 3) ESCAPAR HTML en el resto (< > &) — ahora seguro, el código está fuera
    texto = html.escape(texto, quote=False)

    # 4) negrita **x** y __x__  → <b>
    texto = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", texto, flags=re.DOTALL)
    texto = re.sub(r"__(.+?)__", r"<b>\1</b>", texto, flags=re.DOTALL)
    # 5) cursiva *x*  (sin tocar ** ya consumidas) → <i>
    texto = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", texto)
    # 6) headers markdown (#, ##, ###) → negrita en su línea
    texto = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", texto, flags=re.MULTILINE)
    # 7) enlaces [txt](url) → <a href>
    texto = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2">\1</a>',
        texto,
    )

    # 8) restaurar código inline → <code> (escapando su contenido)
    def _restaurar_inline(m: re.Match) -> str:
        i = int(m.group(1))
        return f"<code>{html.escape(inline[i], quote=False)}</code>"

    texto = re.sub(r"\x00INLINE(\d+)\x00", _restaurar_inline, texto)

    # 9) restaurar bloques → <pre> (escapando su contenido)
    def _restaurar_bloque(m: re.Match) -> str:
        i = int(m.group(1))
        return f"<pre>{html.escape(bloques[i], quote=False)}</pre>"

    texto = re.sub(r"\x00BLOQUE(\d+)\x00", _restaurar_bloque, texto)

    return texto
