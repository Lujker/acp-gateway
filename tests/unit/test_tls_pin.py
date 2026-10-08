"""Trust-on-first-use pin files: atomic, private, validated."""

import os
import stat

import pytest

from acp_gateway.agents import AgentUnavailable, TLSFingerprintMismatch
from acp_gateway.agents.tls import fingerprint_of, normalize_fingerprint, pin_certificate

DER = b"not really a certificate"
ACTUAL = fingerprint_of(DER)


@pytest.fixture(autouse=True)
def fake_peer(monkeypatch):
    async def fetch(host, port, timeout=10):
        return DER

    monkeypatch.setattr("acp_gateway.agents.tls.fetch_peer_certificate", fetch)
    monkeypatch.setattr("acp_gateway.agents.tls.pinned_context", lambda der: None)


async def pin(pin_file):
    return await pin_certificate("agent.local", 3284, configured=None, pin_file=pin_file)


async def test_first_use_saves_a_private_pin(tmp_path):
    pin_file = tmp_path / "pins" / "work.sha256"
    assert (await pin(pin_file)).source == "tofu-new"
    assert pin_file.read_text() == ACTUAL + "\n"
    assert [p.name for p in pin_file.parent.iterdir()] == ["work.sha256"]  # no temporary file left
    if os.name == "posix":
        assert stat.S_IMODE(pin_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(pin_file.parent.stat().st_mode) & 0o077 == 0
    assert (await pin(pin_file)).source == "tofu-saved"


async def test_saved_pin_is_normalized(tmp_path):
    pin_file = tmp_path / "work.sha256"
    pin_file.write_text(ACTUAL.replace(":", "").lower() + "\n")
    assert (await pin(pin_file)).source == "tofu-saved"


async def test_saved_pin_mismatch(tmp_path):
    pin_file = tmp_path / "work.sha256"
    pin_file.write_text("AB" * 32)
    with pytest.raises(TLSFingerprintMismatch):
        await pin(pin_file)


@pytest.mark.parametrize("content", ["", "\n", "garbage", "AB" * 31, "ZZ" * 32])
async def test_corrupt_pin_file_is_reported(tmp_path, content):
    pin_file = tmp_path / "work.sha256"
    pin_file.write_text(content)
    with pytest.raises(AgentUnavailable, match="corrupt TLS pin file"):
        await pin(pin_file)
    assert pin_file.read_text() == content  # never silently re-pinned


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
async def test_unwritable_pin_dir_is_normalized(tmp_path):
    pins = tmp_path / "pins"
    pins.mkdir(mode=0o500)
    try:
        with pytest.raises(AgentUnavailable, match="cannot save TLS pin file"):
            await pin(pins / "work.sha256")
    finally:
        pins.chmod(0o700)


async def test_unreadable_pin_file_is_normalized(tmp_path):
    pin_file = tmp_path / "work.sha256"
    pin_file.mkdir()
    with pytest.raises(AgentUnavailable, match="cannot read TLS pin file"):
        await pin(pin_file)


async def test_interrupted_write_leaves_no_pin(tmp_path, monkeypatch):
    real_replace = os.replace

    def crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("acp_gateway.agents.tls.os.replace", crash)
    pin_file = tmp_path / "pins" / "work.sha256"
    with pytest.raises(AgentUnavailable, match="disk full"):
        await pin(pin_file)
    assert list(pin_file.parent.iterdir()) == []
    monkeypatch.setattr("acp_gateway.agents.tls.os.replace", real_replace)
    assert (await pin(pin_file)).source == "tofu-new"


def test_normalize_fingerprint():
    assert normalize_fingerprint(" " + ACTUAL.lower() + "\n") == ACTUAL
    assert normalize_fingerprint(ACTUAL.replace(":", "")) == ACTUAL
    assert normalize_fingerprint(ACTUAL + "00") is None
