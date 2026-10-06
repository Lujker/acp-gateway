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
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from acp_gateway.agents.errors import AgentUnavailable, TLSFingerprintMismatch

PinSource = Literal["config", "tofu-new", "tofu-saved"]


def fingerprint_of(der: bytes) -> str:
    """SHA-256 of a DER certificate in ``AA:BB:..`` form (as goose prints it)."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


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


async def pin_certificate(
    host: str, port: int, *, configured: str | None, pin_file: Path, timeout: float = 10
) -> TlsPin:
    """Verify the server certificate against the configured or saved pin (TOFU)."""
    der = await fetch_peer_certificate(host, port, timeout)
    actual = fingerprint_of(der)

    source: PinSource
    if configured:
        expected, source = configured, "config"
    elif pin_file.is_file():
        expected, source = pin_file.read_text().strip(), "tofu-saved"
    else:
        pin_file.parent.mkdir(parents=True, exist_ok=True)
        pin_file.write_text(actual + "\n")
        expected, source = actual, "tofu-new"

    if actual != expected:
        raise TLSFingerprintMismatch(
            f"TLS fingerprint mismatch for {host}:{port}: expected {expected}, got {actual}"
        )
    return TlsPin(actual, pinned_context(der), source)
