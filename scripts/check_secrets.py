"""Pre-commit guard: refuse to commit secrets.

Usage:
    python scripts/check_secrets.py --staged      # files staged for commit (git hook)
    python scripts/check_secrets.py FILE...       # explicit files

A line can be exempted with the marker ``secrets-check: ignore``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IGNORE_MARKER = "secrets-check: ignore"

# Files that must never be committed, whatever their content.
# SSH private keys (id_rsa, id_ed25519, ...; the .pub halves are fine) and any *.env.
FORBIDDEN_NAMES = re.compile(
    r"^(\.env(\..+)?|.+\.env|config\.yaml|.+\.(pem|key|p12|pfx)|id_(rsa|dsa|ecdsa|ed25519)(_sk)?)$"
)
ALLOWED_NAMES = {".env.example"}

_NAME = r"[\w-]*(?:secret|token|passw(?:or)?d|api_?key)[\w-]*"
# ${VAR}, <your-secret>, $(cmd), {template}, your-..., change-me
_PLACEHOLDER = r"[<$({]|your[-_]|change[-_]?me"
# an ALL_CAPS value refers to another variable rather than holding a secret
_REFERENCE = r"[A-Z][A-Z0-9_]*(?:[\s'\",]|$)"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # no \b: tokens follow "/bot" in API URLs and may end in "-"
    (
        "telegram bot token",
        re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    ),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{32,}=*")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("anthropic/openai key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    (
        "assigned secret",
        # quoted literal: GOOSE_SERVER__SECRET_KEY='...', api_token = "..."
        re.compile(
            rf"(?i)\b{_NAME}\s*[:=]\s*(['\"])(?!{_PLACEHOLDER})(?-i:(?!{_REFERENCE}))"
            r"[^\s'\"]{12,}\1"
        ),
    ),
    (
        "assigned secret",
        # whole .env / YAML line: TELEGRAM_BOT_TOKEN=..., api_token: ...
        re.compile(
            rf"(?i)^\s*(?:export\s+)?{_NAME}\s*[:=]\s*(?!{_PLACEHOLDER})(?-i:(?!{_REFERENCE}))"
            r"[^\s'\"#()\[\]{},]{12,}\s*(?:#.*)?$"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.kind}"


def scan_text(path: str, text: str) -> list[Finding]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if IGNORE_MARKER in line:
            continue
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(Finding(path, lineno, kind))
                break
    return findings


def scan_name(path: str) -> Finding | None:
    name = PurePosixPath(path).name
    if name not in ALLOWED_NAMES and FORBIDDEN_NAMES.match(name):
        return Finding(path, 0, "file must not be committed")
    return None


def scan(files: Iterable[tuple[str, str | None]]) -> list[Finding]:
    """``files`` yields (path, text); text is None for binary or unreadable files."""
    findings: list[Finding] = []
    for path, text in files:
        if (finding := scan_name(path)) is not None:
            findings.append(finding)
        if text is not None:
            findings.extend(scan_text(path, text))
    return findings


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout  # noqa: S603, S607


def _decode(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _staged_files() -> Iterable[tuple[str, str | None]]:
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
    for raw in filter(None, names):
        path = raw.decode()
        yield path, _decode(_git("show", f":{path}"))


def _explicit_files(paths: Sequence[str]) -> Iterable[tuple[str, str | None]]:
    for path in paths:
        yield path, _decode(Path(path).read_bytes())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="scan files staged for commit")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)

    files = _staged_files() if args.staged else _explicit_files(args.files)
    findings = scan(files)
    for finding in findings:
        print(f"secrets-check: {finding}", file=sys.stderr)
    if findings:
        print(
            f"secrets-check: {len(findings)} problem(s) found; commit refused. "
            f"Mark a false positive with '{IGNORE_MARKER}'.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
