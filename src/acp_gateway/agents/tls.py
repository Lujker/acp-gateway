"""TLS certificate pinning for agents with self-signed certificates (goose serve --tls).

The certificate is first read without trusting it and without sending any
credentials, then compared with the pin. The real connection uses an SSL
context that trusts only that exact certificate, so the agent secret is sent
only inside a handshake that already verified the pin.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from acp_gateway.agents.errors import AgentUnavailable, TLSFingerprintMismatch

PinSource = Literal["config", "tofu-new", "tofu-saved"]

_HEX_FINGERPRINT = re.compile(r"[0-9A-F]{64}")


def fingerprint_of(der: bytes) -> str:
    """SHA-256 of a DER certificate in ``AA:BB:..`` form (as goose prints it)."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def normalize_fingerprint(value: str) -> str | None:
    """``AA:BB:..`` form of a SHA-256 fingerprint, or None if ``value`` is not one."""
    hex_digits = re.sub(r"[\s:]", "", value).upper()
    if not _HEX_FINGERPRINT.fullmatch(hex_digits):
        return None
    return ":".join(hex_digits[i : i + 2] for i in range(0, 64, 2))


async def fetch_peer_certificate(host: str, port: int, timeout: float = 10) -> bytes:
    """Read the server certificate without verifying it. Sends no credentials."""
    probe = ssl.create_default_context()
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=probe, server_hostname=host), timeout=timeout
        )
    except (OSError, TimeoutError) as exc:
        raise AgentUnavailable(f"cannot reach {host}:{port}: {exc or type(exc).__name__}") from exc
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if not der:
        raise AgentUnavailable(f"{host}:{port} presented no TLS certificate")
    return der


@dataclass(frozen=True)
class TlsPin:
    fingerprint: str
    context: ssl.SSLContext
    source: PinSource


def pinned_context(der: bytes) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
    # The certificate is self-signed for an arbitrary name; the pin is the identity.
    context.check_hostname = False
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


def _read_pin(pin_file: Path) -> str | None:
    try:
        text = pin_file.read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentUnavailable(f"cannot read TLS pin file {pin_file}: {exc}") from exc
    pin = normalize_fingerprint(text)
    if pin is None:
        raise AgentUnavailable(f"corrupt TLS pin file {pin_file}: expected a SHA-256 fingerprint")
    return pin


def _save_pin(pin_file: Path, fingerprint: str) -> None:
    """Write via a private temporary file so a crash never leaves a partial pin."""
    tmp = pin_file.with_name(f".{pin_file.name}.{secrets.token_hex(4)}.tmp")
    try:
        pin_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            try:
                os.write(fd, f"{fingerprint}\n".encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            tmp.replace(pin_file)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
    except OSError as exc:
        raise AgentUnavailable(f"cannot save TLS pin file {pin_file}: {exc}") from exc


async def pin_certificate(
    host: str, port: int, *, configured: str | None, pin_file: Path, timeout: float = 10
) -> TlsPin:
    """Verify the server certificate against the configured or saved pin (TOFU)."""
    der = await fetch_peer_certificate(host, port, timeout)
    actual = fingerprint_of(der)

    source: PinSource
    if configured:
        expected, source = configured, "config"
    elif (saved := _read_pin(pin_file)) is not None:
        expected, source = saved, "tofu-saved"
    else:
        _save_pin(pin_file, actual)
        expected, source = actual, "tofu-new"

    if actual != expected:
        raise TLSFingerprintMismatch(
            f"TLS fingerprint mismatch for {host}:{port}: expected {expected}, got {actual}"
        )
    return TlsPin(actual, pinned_context(der), source)
