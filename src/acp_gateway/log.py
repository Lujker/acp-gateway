"""Structured logging with secret redaction.

Both structlog loggers and plain stdlib loggers (httpx, websockets, ...) go
through the same redaction step, so a secret cannot leak through a
third-party library message either.

Redaction works on three levels:
1. values under sensitive keys (``secret``, ``token``, ``authorization``...);
2. exact values registered at runtime via :func:`register_secret`
   (agent secrets, bot tokens) — wherever they appear in a string;
3. well-known textual shapes: ``token=...`` in URLs, ``Bearer ...``,
   ``X-Secret-Key: ...``.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Iterable, MutableMapping
from typing import Any, Literal, TextIO

import structlog

MASK = "***"
_MIN_SECRET_LENGTH = 6

_SENSITIVE_KEY = re.compile(
    r"(secret|token|passw|api[_-]?key|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ?token=..., &secret=..., key=... in URLs and query strings
    (
        re.compile(r"(?i)\b([\w-]*(?:token|secret|key|passw\w*))=([^&\s'\"]+)"),
        rf"\1={MASK}",
    ),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {MASK}"),
    (re.compile(r"(?i)\b(x-secret-key)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), rf"\1\2{MASK}"),
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


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, MutableMapping):
        return {k: _redact_item(k, v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return type(value)(_redact_value(v) for v in value)
    return value


def _redact_item(key: Any, value: Any) -> Any:
    if isinstance(key, str) and _SENSITIVE_KEY.search(key) and value not in (None, ""):
        return MASK
    return _redact_value(value)


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


def configure_logging(
    level: str = "INFO",
    fmt: LogFormat = "auto",
    stream: TextIO | None = None,
    extra_secrets: Iterable[str] = (),
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

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
