"""Source footers reserve Telegram's UTF-16 budget without changing answer content."""

# ruff: noqa: RUF001 -- Russian UI labels

import secrets

from acp_gateway.channels.telegram_format import plain_html, result_chunks
from acp_gateway.log import register_secret
from fakes.telegram import answer_body


def test_signed_unicode_chunks_keep_all_body_content_and_footer():
    source = "😀<&> " * 1000
    footer = "Ответ из «Диалог» #42\nCodex · модель неизвестна\nПапка: /work"
    chunks = result_chunks(source, footer=footer)
    bodies = []
    for chunk in chunks:
        text = plain_html(chunk)
        assert "Ответ из «Диалог» #42" in text
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        bodies.append(plain_html(answer_body(chunk)).split("\n\n", 1)[1])
    assert "".join(bodies) == source.rstrip()


def test_source_values_are_escaped_and_redacted_before_rendering():
    credential = secrets.token_hex(30)
    register_secret(credential)
    chunks = result_chunks("**answer**", footer="Source <script> " + credential)
    assert len(chunks) == 1
    assert credential not in chunks[0] and "&lt;script&gt;" in chunks[0]
    assert answer_body(chunks[0]) == "<b>answer</b>"
