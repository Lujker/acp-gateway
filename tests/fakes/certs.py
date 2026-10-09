"""Self-signed TLS fixtures with a small allowance for test-host clock skew."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def make_cert(
    directory: Path, *, ca: bool = False, hostname: str | None = None, now: datetime | None = None
) -> tuple[str, str]:
    """Create a leaf or CA fixture; ``now`` allows validity-boundary tests.

    WSL/OpenSSL clocks have differed by 27 seconds during live smoke runs.
    Backdate only these disposable fixtures; production TLS still checks dates.
    """
    now = now or datetime.now(UTC)
    cert, key = directory / f"cert-{ca}.pem", directory / f"key-{ca}.pem"
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-goose")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(private.public_key()), critical=False
        )
    )
    if hostname:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
    cert.write_bytes(
        builder.sign(private, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    )
    with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as file:
        file.write(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return str(cert), str(key)


def fingerprint(cert_path: str) -> str:
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
