"""Deterministic Markdown-to-Telegram HTML with independently valid chunks."""

# ruff: noqa: RUF001 -- Russian system labels and emoji

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from acp_gateway.log import MASK, redact_text

FORMAT_VERSION = "telegram-markdown-v2"
SOURCE_FORMAT_VERSION = "telegram-markdown-v3-source"
LEGACY_FORMAT_VERSION = "telegram-markdown-v1"
_MASK_MARKER = "\ue000ACP_MASK\ue001"
LIMIT = 2000  # At most 4000 UTF-16 units, below Telegram's entity-parsed limit.
_PARSER = MarkdownIt("commonmark", {"html": False, "maxNesting": 20}).enable(
    ["table", "strikethrough"]
)


def _url(value):
    value = redact_text(
        value.replace(_MASK_MARKER, MASK).replace("%EE%80%80ACP_MASK%EE%80%81", MASK)
    )
    try:
        return value if len(value) <= 2048 and urlsplit(value).scheme in {"http", "https"} else None
    except ValueError:
        return None


class _Chunks:
    def __init__(self, *, limit=LIMIT, boundaries=False):
        self.limit, self.boundaries = limit, boundaries
        self.stack = []
        self.parts = []
        self.plain = []
        self.result = []
        self.size = 0
        self.total = 0
        self.pending = 0

    def separate(self, count=2):
        self.pending = max(self.pending, count)

    def _flush(self):
        if any(part.strip() for part in self.plain):
            self.result.append(
                "".join(self.parts) + "".join(end for _, end in reversed(self.stack))
            )
        self.parts = [start for start, _ in self.stack]
        self.plain = []
        self.size = 0

    def _text(self, value):
        while value:
            if self.size == self.limit:
                self._flush()
            available = self.limit - self.size
            take = min(len(value), available)
            if self.boundaries and len(value) > available:
                for separator in ("\n\n", "\n", " "):
                    boundary = value.rfind(separator, 0, available)
                    if boundary >= available // 2:
                        take = boundary + len(separator)
                        break
            piece, value = value[:take], value[take:]
            self.parts.append(html.escape(piece, quote=False))
            self.plain.append(piece)
            self.size += len(piece)
            self.total += len(piece)
            if value:
                self._flush()

    def _separation(self):
        if self.pending and self.total:
            self._text("\n" * self.pending)
        self.pending = 0

    def text(self, value):
        if not value:
            return
        self._separation()
        self._text(value.replace(_MASK_MARKER, MASK))

    def open(self, tag, attributes=""):
        self._separation()
        start, end = f"<{tag}{attributes}>", f"</{tag}>"
        self.parts.append(start)
        self.stack.append((start, end))

    def close(self):
        _, end = self.stack.pop()
        self.parts.append(end)

    def code(self, value, language=None, *, block=False):
        # Telegram code/pre entities cannot overlap bold/italic/link entities.
        saved = list(self.stack)
        while self.stack:
            self.close()
        if block:
            self.open("pre")
        attributes = f' class="language-{html.escape(language, quote=True)}"' if language else ""
        self.open("code", attributes if block else "")
        self.text(value)
        self.close()
        if block:
            self.close()
        for start, end in saved:
            self.parts.append(start)
            self.stack.append((start, end))

    def finish(self):
        self._flush()
        return self.result or ["(empty response)"]


def _inline(tokens, out):
    links = []
    for token in tokens:
        kind = token.type
        if kind == "text":
            out.text(token.content)
        elif kind in {"softbreak", "hardbreak"}:
            out.text("\n")
        elif kind == "code_inline":
            out.code(token.content)
        elif kind in {"strong_open", "em_open", "s_open"}:
            out.open({"strong_open": "b", "em_open": "i", "s_open": "s"}[kind])
        elif kind in {"strong_close", "em_close", "s_close"}:
            out.close()
        elif kind == "link_open":
            url = _url(token.attrGet("href") or "")
            links.append(bool(url))
            if url:
                out.open("a", f' href="{html.escape(url, quote=True)}"')
        elif kind == "link_close":
            if links.pop():
                out.close()
        elif kind == "image":
            out.text("Image: " + (token.content or "image"))
            url = _url(token.attrGet("src") or "")
            if url:
                out.text(f" ({url})")
        else:
            out.text(token.content)


def _table(tokens, index, out):
    rows, row, cell = [], [], []
    while index < len(tokens):
        token = tokens[index]
        if token.type == "tr_open":
            row = []
        elif token.type == "inline":
            cell = [t.content for t in token.children or [] if t.nesting == 0]
        elif token.type in {"th_close", "td_close"}:
            row.append(" ".join("".join(cell).split()))
            cell = []
        elif token.type == "tr_close":
            rows.append(row)
        elif token.type == "table_close":
            break
        index += 1
    if rows:
        widths = [min(40, max(len(row[i]) for row in rows)) for i in range(len(rows[0]))]
        lines = [" | ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) for row in rows]
        lines.insert(1, "-+-".join("-" * w for w in widths))
        out.code("\n".join(lines), block=True)
        out.separate()
    return index


def _render_answer(text, *, already_redacted=False, boundaries=False, limit=None):
    """Redact before parsing; raw HTML is always literal, never trusted markup."""
    redacted = redact_text(text)
    text = (
        redacted.replace(MASK, _MASK_MARKER) if already_redacted or redacted != text else redacted
    )
    out = _Chunks(limit=limit or (1900 if boundaries else LIMIT), boundaries=boundaries)
    lists, quote_depth = [], 0
    tokens = _PARSER.parse(text)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "inline":
            _inline(token.children or [], out)
        elif kind in {"fence", "code_block"}:
            language = token.info.split()[0] if token.info.strip() else None
            if language and not re.fullmatch(r"[A-Za-z0-9_+.#-]{1,32}", language):
                language = None
            out.code(token.content, language, block=True)
            out.separate()
        elif kind == "heading_open":
            out.open("b")
        elif kind == "heading_close":
            out.close()
            out.separate()
        elif kind == "paragraph_close":
            out.separate(1 if lists else 2)
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append(int(token.attrGet("start") or 1) if kind == "ordered_list_open" else None)
        elif kind == "list_item_open":
            out.separate(1)
            number = lists[-1]
            out.text("  " * (len(lists) - 1) + (f"{number}. " if number is not None else "• "))
            if number is not None:
                lists[-1] += 1
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
            out.separate()
        elif kind == "blockquote_open":
            if quote_depth == 0:
                out.open("blockquote")
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
            if quote_depth == 0:
                out.close()
            out.separate()
        elif kind == "hr":
            out.text("────────")
            out.separate()
        elif kind == "table_open":
            index = _table(tokens, index, out)
        index += 1
    return out.finish()


def answer_chunks(text, *, already_redacted=False):
    """The v1 rendering, retained verbatim for deliveries that already started."""
    return _render_answer(text, already_redacted=already_redacted)


def result_chunks(text, *, footer=None):
    """Readable v2 boundaries and part numbers, with space reserved for the title."""
    suffix = ""
    limit = 1900
    if footer:
        clean = redact_text(footer)
        suffix = "\n\n────────────────────\n" + system_chunks(clean)[0]
        limit -= len(plain_html(suffix))
        if limit < 100:
            raise ValueError("result footer exceeds presentation budget")
    chunks = _render_answer(text, boundaries=True, limit=limit)
    if len(chunks) == 1:
        return [chunks[0] + suffix]
    return [
        f"<b>📄 Ответ · {i}/{len(chunks)}</b>\n\n{body}{suffix}" for i, body in enumerate(chunks, 1)
    ]


_FIELDS = {
    "Агент": ("🖥", "connection"),
    "Агент / ПК": ("🖥", "connection"),
    "Профиль агента": ("⚙️", "connection"),
    "Компьютер": ("💻", "connection"),
    "Подключение ПК": ("🔌", "connection"),
    "Подключение агента": ("🔌", "connection"),
    "Готовность агента": ("🔎", "connection"),
    "Профиль": ("⚙️", "connection"),
    "Папка": ("📁", "workspace"),
    "Папка новых диалогов": ("📁", "workspace"),
    "Диалог": ("💬", "dialogue"),
    "Состояние": ("📍", "dialogue"),
    "Выполнение": ("⚙️", "dialogue"),
    "Режим": ("⚙️", "dialogue"),
    "Название": ("🏷", "dialogue"),
    "Модель": ("🧠", "dialogue"),
    "Задача": ("📋", "job"),
    "Страница": ("📑", "job"),
    "Начало": ("🕘", "time"),
    "Завершение": ("🕘", "time"),
    "Длительность": ("⏱", "time"),
    "Ошибка": ("⚠️", "detail"),
    "Пояснение": ("ℹ️", "detail"),
}
_TITLES = {
    "Управление": "🏠",
    "Текущий контекст": "📍",
    "Компьютеры": "💻",
    "Компьютер": "💻",
    "Агенты": "🖥",
    "Профили": "⚙️",
    "Профиль": "⚙️",
    "Задач": "📋",
    "Диалог": "💬",
    "Модель": "🧠",
    "Подтверж": "✅",
    "Обновлен": "⬆️",
    "Действие не": "⚠️",
}

_SECTIONS = {
    "📍 Навигация",
    "💻 Компьютеры и агенты",
    "💬 Диалоги",
    "📋 Задачи и результаты",
    "⚙️ Настройки диалога",
    "💻 Компьютеры",
    "🖥 Агенты",
}


def system_chunks(text):
    """Trusted presentation of plain system text; all values remain escaped/redacted."""
    out = _Chunks(boundaries=True)
    group = None
    for index, line in enumerate(redact_text(text).splitlines()):
        if not line.strip():
            out.separate(2)
            group = None
            continue
        if (
            index == 0
            or line in _SECTIONS
            or line.startswith(("Текущий контекст ·", "🧭 Ход работы", "📄 Ответ"))
        ):
            out.separate(2)
            icon = next((v for k, v in _TITLES.items() if line.startswith(k)), "")
            out.open("b")
            out.text((icon + " " if icon else "") + line)
            out.close()
            out.separate(2)
            continue
        name, separator, value = line.partition(": ")
        if separator and name in _FIELDS:
            icon, new_group = _FIELDS[name]
            out.separate(2 if group is not None and group != new_group else 1)
            out.text(icon + " ")
            out.open("b")
            out.text(name + ":")
            out.close()
            out.text(" " + value)
            group = new_group
        else:
            out.separate(1)
            out.text(line)
    return out.finish()


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_html(chunk):
    """Only for our rendered payload after Telegram explicitly rejects entities."""
    parser = _PlainHTML()
    parser.feed(chunk)
    parser.close()
    return "".join(parser.parts)
