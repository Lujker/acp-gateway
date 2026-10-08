"""Self-signed certificates for TLS tests (needs the openssl CLI)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def make_cert(directory: Path, *, ca: bool = False, hostname: str | None = None) -> tuple[str, str]:
    """Create a self-signed certificate; ``ca=False`` mimics a leaf (CA:FALSE) cert."""
    cert, key = directory / f"cert-{ca}.pem", directory / f"key-{ca}.pem"
    constraints = "critical,CA:TRUE" if ca else "critical,CA:FALSE"
    extra = ["-addext", f"subjectAltName=DNS:{hostname}"] if hostname else []
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=fake-goose", "-addext", f"basicConstraints={constraints}",
            "-keyout", str(key), "-out", str(cert),
            *extra,
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return str(cert), str(key)


def fingerprint(cert_path: str) -> str:
    result = subprocess.run(
        ["openssl", "x509", "-in", cert_path, "-noout", "-fingerprint", "-sha256"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().split("=", 1)[1]
