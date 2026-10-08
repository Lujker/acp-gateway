import io
import json
import logging

import pytest

from acp_gateway.log import (
    MASK,
    configure_logging,
    get_logger,
    redact_text,
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
    ],
)
def test_textual_patterns(text, leaked):
    assert leaked not in redact_text(text)


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
