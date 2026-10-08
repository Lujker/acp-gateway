import io
import json
import logging
import time
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from acp_gateway.log import (
    MASK,
    configure_logging,
    get_logger,
    redact_text,
    redact_value,
    register_secret,
)

SECRET = "goose-" + "shared-secret-42"


@pytest.fixture
def output():
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream, extra_secrets=[SECRET])
    return stream


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_registered_secret_is_masked_everywhere(output):
    logger = get_logger("test")
    logger.info(f"connecting with {SECRET}", header=SECRET, nested={"items": [SECRET]})
    text = output.getvalue()
    assert SECRET not in text
    record = lines(output)[0]
    assert record["event"] == f"connecting with {MASK}"
    assert record["nested"] == {"items": [MASK]}


def test_sensitive_keys_are_masked(output):
    get_logger("test").info(
        "auth", api_token="plain-tok1", password="pw-123456", authorization="Basic xyz"
    )
    record = lines(output)[0]
    assert record["api_token"] == MASK
    assert record["password"] == MASK
    assert record["authorization"] == MASK


def test_secrets_in_mapping_keys_are_redacted(output):
    secret = "opaque-" + "value-123456"
    register_secret(secret)
    get_logger("test").info("keys", **{secret: "value"}, nested={secret: "plain"})
    assert secret not in output.getvalue()
    record = lines(output)[0]
    assert record[MASK] == "value"
    assert record["nested"] == {MASK: "plain"}
    assert redact_value({secret.encode(): "plain"}) == {MASK: "plain"}


def test_empty_sensitive_values_are_kept(output):
    get_logger("test").info("config", api_token_env="ACPGW_API_TOKEN", secret=None)
    record = lines(output)[0]
    # A *name* of an env var is not a secret, but the key matches; only non-empty values mask.
    assert record["secret"] is None


def test_stdlib_loggers_are_redacted(output):
    logging.getLogger("httpx").warning("GET wss://host/acp?token=%s failed", SECRET)
    text = output.getvalue()
    assert SECRET not in text
    assert lines(output)[0]["logger"] == "httpx"


def test_exception_text_is_redacted(output):
    try:
        raise RuntimeError(f"handshake failed for key {SECRET}")
    except RuntimeError:
        get_logger("test").exception("boom")
        logging.getLogger("lib").exception("lib boom")
    assert SECRET not in output.getvalue()
    assert "handshake failed" in output.getvalue()


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("wss://h/acp?token=abc123&x=1", "abc123"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi.payload.sig"),
        ("headers={'X-Secret-Key': 'k3y-value-zz'}", "k3y-value-zz"),
        ("x-secret-key=k3y-value-zz", "k3y-value-zz"),
        ("wss://alice:hunter2pass@host:3284/acp", "hunter2pass"),
        ("connect https://bot:pa:ss@api.example/x failed", "pa:ss"),
        ("""{"token": "tok_abcdef123456", "other": 1}""", "tok_abcdef123456"),
        ("{'api_key': 'abc\\'def123456', 'password': 'pw123456'}", "def123456"),
        ("{'password': 'pw123456'}", "pw123456"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("authorization=Token abcdef0123456789", "abcdef0123456789"),
    ],
)
def test_textual_patterns(text, leaked):
    assert leaked not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "wss://work.lan:3284/acp",
        "ssh://git@github.com/org/repo",
        """{"title": "Run tests", "kind": "execute"}""",
        "invalid token received",
    ],
)
def test_textual_patterns_keep_harmless_text(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "a." * 50_000,
        '"' + "token" * 20_000,
        '"a":\'' + "x" * 100_000,
        "a://" + "b" * 100_000,
        "Authorization: Basic " + "a" * 100_000,
        "wss://" + "u" * 100_000,
    ],
)
def test_textual_patterns_are_linear(text):
    started = time.monotonic()
    redact_text(text)
    assert time.monotonic() - started < 1


def test_non_text_values_are_redacted(output):
    class Opaque:
        def __repr__(self):
            return f"Opaque(key {SECRET})"

    get_logger("test").info(
        "values",
        error=ValueError(f"auth failed with {SECRET}"),
        payload=SECRET.encode(),
        buffer=bytearray(SECRET.encode()),
        thing=Opaque(),
        count=3,
        ratio=0.5,
        flag=True,
        nothing=None,
    )
    assert SECRET not in output.getvalue()
    record = lines(output)[0]
    assert record["error"] == f"ValueError('auth failed with {MASK}')"
    assert record["payload"] == MASK
    assert (record["count"], record["ratio"], record["flag"], record["nothing"]) == (
        3,
        0.5,
        True,
        None,
    )


def test_non_text_redaction_keeps_exception_rendering(output):
    try:
        raise RuntimeError(f"failure {SECRET}")
    except RuntimeError as exc:
        get_logger("test").error("failed", exc_info=exc)
    record = lines(output)[0]
    assert "Traceback" in record["exception"]
    assert f"RuntimeError: failure {MASK}" in record["exception"]


def test_named_tuples_and_tuple_subclasses_are_rebuilt():
    register_secret(SECRET)
    split = redact_value(urlsplit(f"wss://h/acp?token={SECRET}"))
    assert split.query == f"token={MASK}"
    assert split.netloc == "h"
    Pair = namedtuple("Pair", "key value")
    assert redact_value(Pair("k", SECRET)) == Pair("k", MASK)
    stat = redact_value(Path().stat())
    assert isinstance(stat, tuple)
    assert stat[0] == Path().stat().st_mode


def test_deep_nesting_is_masked_instead_of_overflowing():
    nested = current = {}
    for _ in range(5_000):
        current["child"] = {}
        current = current["child"]
    current["leaf"] = "visible"
    redacted = redact_value(nested)
    depth = 0
    while isinstance(redacted, dict):
        redacted = redacted["child"]
        depth += 1
    assert redacted == MASK
    assert depth <= 64
    shallow = redact_value({"a": [{"b": ("c", "plain")}]})
    assert shallow == {"a": [{"b": ("c", "plain")}]}


def test_short_values_are_not_registered():
    register_secret("abc")
    assert redact_text("abc def") == "abc def"


def test_level_filtering():
    stream = io.StringIO()
    configure_logging(level="WARNING", fmt="json", stream=stream)
    get_logger("test").info("hidden")
    get_logger("test").warning("shown")
    assert [r["event"] for r in lines(stream)] == ["shown"]


def test_file_rotation_is_bounded_private_json_and_redacts_all_handlers(tmp_path):
    stream = io.StringIO()
    path = tmp_path / "private" / "gateway.log"
    configure_logging(
        fmt="console",
        stream=stream,
        extra_secrets=[SECRET],
        file=path,
        max_bytes=800,
        backup_count=2,
    )
    for number in range(20):
        get_logger("rotation").warning("line", number=number, value=SECRET, payload="x" * 150)
    logging.getLogger("foreign").warning("Bearer %s", SECRET)
    files = sorted(path.parent.glob("gateway.log*"))
    assert len(files) == 3
    for log in files:
        assert log.stat().st_mode & 0o777 == 0o600
        assert SECRET not in log.read_text()
        assert all(isinstance(json.loads(line), dict) for line in log.read_text().splitlines())
    assert SECRET not in stream.getvalue()
    configure_logging(stream=stream)


def test_log_file_does_not_follow_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserved\n")
    link = tmp_path / "log"
    link.symlink_to(target)
    with pytest.raises(OSError):
        configure_logging(file=link)
    assert target.read_text() == "preserved\n"
