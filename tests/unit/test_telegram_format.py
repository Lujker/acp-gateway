# ruff: noqa: RUF001, S108 -- rendered labels and path strings only
from html.parser import HTMLParser

import pytest

from acp_gateway.channels.telegram_format import answer_chunks, result_chunks, system_chunks
from acp_gateway.log import MASK, register_secret


class Parsed(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "s", "a", "pre", "code", "blockquote"}
        assert not (tag == "blockquote" and tag in self.stack)
        if tag in {"pre", "code"}:
            assert not any(t in self.stack for t in {"b", "i", "s", "a", "blockquote"})
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text.append(data)


def readable(chunk):
    parsed = Parsed()
    parsed.feed(chunk)
    parsed.close()
    assert not parsed.stack
    text = "".join(parsed.text)
    assert len(text.encode("utf-16-le")) // 2 <= 4000
    return text


def test_markdown_code_links_lists_and_quotes_use_supported_html():
    source = (
        "# Title\n\n**bold** *italic* ~~old~~ `a < b`\n\n"
        "[site](https://example.com?q=a&b=2)\n\n- one\n- two\n\n"
        "> quoted\n\n```python\nprint('<tag>')\n```"
    )
    output = "".join(answer_chunks(source))
    assert "<b>Title</b>" in output and "<i>italic</i>" in output and "<s>old</s>" in output
    assert "<code>a &lt; b</code>" in output
    assert '<a href="https://example.com?q=a&amp;b=2">site</a>' in output
    assert "<blockquote>" in output and '<code class="language-python">' in output
    assert "• one" in readable(output) and "print('<tag>')" in readable(output)


@pytest.mark.parametrize("style", ["**", "*", "~~", "```python\n"])
def test_long_unicode_chunks_close_and_reopen_entities_without_losing_content(style):
    body = ("😀<&> " * 1000).rstrip()
    ending = "\n```" if style.startswith("```") else style
    chunks = answer_chunks(style + body + ending)
    text = "".join(readable(chunk) for chunk in chunks)
    assert len(chunks) > 2
    assert text.rstrip("\n") == body


def test_code_inside_style_and_nested_quotes_are_valid_telegram_entities():
    output = answer_chunks(
        "**before `x < y` after**\n\n> outer\n>\n>> inner\n>\n> ```js\n> code()\n> ```"
    )
    for chunk in output:
        readable(chunk)
    assert "<code>x &lt; y</code>" in "".join(output)


def test_raw_html_dangerous_links_and_secrets_are_never_trusted():
    credential = "test-render-credential-54321"
    register_secret(credential)
    output = "".join(
        answer_chunks(
            f"<script>alert(1)</script>\n[{credential}](https://example.com/?token={credential})\n[x](javascript:alert(1))"
        )
    )
    assert credential not in output and MASK in output
    assert "<script>" not in output and "&lt;script&gt;" in output
    assert 'href="javascript:' not in output
    readable(output)


def test_table_preserves_cells_as_code_and_images_are_links_without_fetching():
    source = "| Name | Value |\n| --- | --- |\n| a | <one> |\n| b | **two** |\n\n![image](https://example.com/image.png)"
    output = "".join(answer_chunks(source))
    assert "<pre><code>" in output and "<table>" not in output
    text = readable(output)
    assert "<one>" in text and "two" in text and "https://example.com/image.png" in text


@pytest.mark.parametrize("source", ["", "  \n", "**unfinished", "```\nnot closed", "]](", "a & b"])
def test_incomplete_or_empty_markdown_remains_deliverable(source):
    for chunk in answer_chunks(source):
        assert readable(chunk).strip()


def test_already_redacted_preview_keeps_visible_mask():
    assert "***" in readable("".join(answer_chunks("Secret: ***", already_redacted=True)))


def test_system_cards_emphasize_sections_and_escape_values():
    chunks = system_chunks("Управление ACP Gateway\n\nАгент: <script>\nПапка: /tmp/a&b\nДиалог: #7")
    assert "<b>🏠 Управление ACP Gateway</b>" in chunks[0]
    assert "<b>Агент:</b> &lt;script&gt;" in chunks[0]
    assert "\n\n📁" in readable(chunks[0])
    assert "/tmp/a&b" in readable(chunks[0])


def test_multipart_replies_number_parts_without_losing_content():
    source = ("paragraph words " * 500).rstrip()
    chunks = result_chunks(source)
    bodies = []
    for index, chunk in enumerate(chunks, 1):
        text = readable(chunk)
        title = f"📄 Ответ · {index}/{len(chunks)}\n\n"
        assert text.startswith(title)
        bodies.append(text[len(title) :])
    assert "".join(bodies) == source
    assert all(body.endswith(" ") for body in bodies[:-1])


@pytest.mark.parametrize("style", ["**", "*", "~~", "```python\n"])
def test_numbered_unicode_parts_keep_valid_entities_and_utf16_budget(style):
    body = ("😀<&> " * 1000).rstrip()
    ending = "\n```" if style.startswith("```") else style
    chunks = result_chunks(style + body + ending)
    texts = [readable(chunk).split("\n\n", 1)[1] for chunk in chunks]
    assert "".join(texts).rstrip("\n") == body
