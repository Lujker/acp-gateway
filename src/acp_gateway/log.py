"""Structured logging with secret redaction.

Both structlog loggers and plain stdlib loggers (httpx, websockets, ...) go
through the same redaction step, so a secret cannot leak through a
third-party library message either.

Redaction works on three levels:
1. values under sensitive keys (``secret``, ``token``, ``authorization``...);
2. exact values registered at runtime via :func:`register_secret`
   (agent secrets, bot tokens) — wherever they appear in a string;
3. well-known textual shapes: ``token=...`` in URLs, ``Bearer ...``,
   ``X-Secret-Key: ...``, ``scheme://user:pass@``, ``"api_key": "..."``.

Values that are neither text nor containers (exceptions, bytes, arbitrary
objects) are rendered to text and redacted, since renderers would ``repr()``
them unmasked.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal, TextIO

import structlog

MASK = "***"
_MIN_SECRET_LENGTH = 6
# Agent-controlled data (raw_input) can nest arbitrarily; deeper values are masked.
_MAX_DEPTH = 64

_SENSITIVE_KEY = re.compile(
    r"(secret|token|passw|api[_-]?key|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)


def _mask_quoted_pair(match: re.Match[str]) -> str:
    quote, key, separator, value_quote, value = match.groups()
    if not value or not _SENSITIVE_KEY.search(key):
        return match.group(0)
    return f"{quote}{key}{quote}{separator}{value_quote}{MASK}{value_quote}"


# Quantifiers are bounded or possessive so hostile input cannot backtrack catastrophically.
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    # ?token=..., &secret=..., key=... in URLs and query strings
    (
        re.compile(r"(?i)\b([\w-]*(?:token|secret|key|passw\w*))=([^&\s'\"]+)"),
        rf"\1={MASK}",
    ),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {MASK}"),
    (re.compile(r"(?i)\b(x-secret-key)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), rf"\1\2{MASK}"),
    # Authorization: Basic dXNlcjpwYXNz / Token abc (Bearer is masked above)
    (
        re.compile(
            r"(?i)\b(authorization['\"]?\s*+[:=]\s*+['\"]?(?:basic|token|digest|negotiate)\s++)"
            r"[^\s'\",}]++"
        ),
        rf"\1{MASK}",
    ),
    # wss://user:password@host -> wss://***@host
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}://)[^\s/@:'\"]{1,256}:[^\s/@'\"]{0,512}@"),
        rf"\1{MASK}@",
    ),
    # "token": "...", 'api_key': '...' in JSON and dict reprs
    (
        re.compile(r"""(["'])([\w-]++)\1(\s*+[:=]\s*+)(["'])((?:(?!\4)[^\\]|\\.)*+)\4"""),
        _mask_quoted_pair,
    ),
)

_registry: set[str] = set()
_registry_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Mask this exact value in every log line from now on."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        with _registry_lock:
            _registry.add(value)


def clear_registered_secrets() -> None:
    with _registry_lock:
        _registry.clear()


def redact_text(text: str) -> str:
    with _registry_lock:
        secrets = sorted(_registry, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= _MAX_DEPTH:
        return MASK
    if isinstance(value, Mapping):
        return {k: _redact_item(k, v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return _rebuild(value, [_redact_value(v, depth + 1) for v in value])
    if isinstance(value, bytes | bytearray | memoryview):
        return redact_text(bytes(value).decode("utf-8", "replace"))
    # Exceptions and arbitrary objects: renderers would repr() them unredacted.
    try:
        text = repr(value)
    except Exception:  # a broken __repr__ must not break logging
        text = f"<unrepresentable {type(value).__name__}>"
    return redact_text(text)


def _rebuild(value: list | tuple | set | frozenset, items: list[Any]) -> Any:
    kind = type(value)
    if kind in (list, tuple, set, frozenset):
        return kind(items)
    if isinstance(value, tuple) and hasattr(kind, "_fields"):
        try:
            return kind(*items)  # namedtuple, e.g. SplitResult
        except TypeError:
            pass
    for base in (list, tuple, set, frozenset):
        if isinstance(value, base):
            return base(items)
    return items


def _redact_item(key: Any, value: Any, depth: int = 0) -> Any:
    if isinstance(key, str) and _SENSITIVE_KEY.search(key) and value not in (None, ""):
        return MASK
    return _redact_value(value, depth)


def redact_value(value: Any) -> Any:
    """Return a redacted copy of structured data, including nested sensitive keys."""
    return _redact_value(value)


_STRUCTLOG_KEYS = frozenset({"_record", "_from_structlog"})


def redact_secrets(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> Any:
    """structlog processor: mask secrets everywhere in the event dict."""
    for key in list(event_dict):
        if key in _STRUCTLOG_KEYS:
            continue
        if key == "event":
            event_dict[key] = _redact_value(event_dict[key])
        else:
            event_dict[key] = _redact_item(key, event_dict[key])
    return event_dict


LogFormat = Literal["auto", "console", "json"]
_configured_handlers: list[logging.Handler] = []


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def configure_logging(
    level: str = "INFO",
    fmt: LogFormat = "auto",
    stream: TextIO | None = None,
    extra_secrets: Iterable[str] = (),
    *,
    file: Path | None = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
) -> None:
    """Configure structlog and stdlib logging to write redacted logs to ``stream``."""
    for secret in extra_secrets:
        register_secret(secret)

    stream = stream or sys.stderr
    if fmt == "auto":
        fmt = "console" if stream.isatty() else "json"
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=stream.isatty())
    )

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        # Exceptions are rendered to text before redaction so their messages are masked too.
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            redact_secrets,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder(), redact_secrets],
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [handler]
    if file is not None:
        file = Path(file).absolute()
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rotating = _PrivateRotatingFileHandler(
            file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        # File logs are always JSON; terminal rendering remains independent.
        rotating.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder(), redact_secrets],
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
            )
        )
        handlers.append(rotating)

    root = logging.getLogger()
    root.handlers = handlers
    for previous in _configured_handlers:
        previous.close()
    _configured_handlers[:] = handlers
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
