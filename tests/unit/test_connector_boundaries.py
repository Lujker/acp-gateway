"""Validate URL and credential boundaries without networking."""

import os

import pytest

from acp_gateway.connectors.control import normalize_pin, read_credential, validate_url
from acp_gateway.storage import Store


@pytest.mark.parametrize(
    "url",
    [
        "ws://localhost/connect",
        "wss://user:secret@localhost/connect",
        "wss://localhost/connect?token=x",
        "wss://localhost/connect#fragment",
        "wss://localhost/other",
        "wss:///connect",
        "wss://localhost:bad/connect",
        "wss://localhost:0/connect",
        "wss://localhost:65536/connect",
    ],
)
def test_unsafe_urls_rejected_without_echo(url):
    with pytest.raises(ValueError) as error:
        validate_url(url)
    assert url not in str(error.value)


def test_normalize_pin_and_url():
    assert validate_url("wss://localhost:8766/connect").port == 8766
    assert normalize_pin("ab" * 32) == ":".join(["AB"] * 32)
    with pytest.raises(ValueError):
        normalize_pin("wrong")


def test_credential_reader_rejects_public_symlink_directory_and_fifo(tmp_path):
    store = Store.open(":memory:")
    try:
        key = tmp_path / "key"
        store.computers.issue("work", key, display_name="Work")
        credential = read_credential(key)
        assert store.computers.authenticate("work", credential)
        key.chmod(0o644)
        with pytest.raises(ValueError, match="private permissions"):
            read_credential(key)
        key.chmod(0o600)
        link = tmp_path / "link"
        link.symlink_to(key)
        with pytest.raises(OSError):
            read_credential(link)
        with pytest.raises((ValueError, OSError)):
            read_credential(tmp_path)
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo, 0o600)
        with pytest.raises(ValueError, match="regular file"):
            read_credential(fifo)
    finally:
        store.close()
