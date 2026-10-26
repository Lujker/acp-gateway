"""Bounded connector-owned discovery. Only opaque capabilities leave this module."""

import asyncio
import contextlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

from acp_gateway.config import AgentProfile, WslSettings
from acp_gateway.connectors import _discovery_probe
from acp_gateway.connectors.protocol import AgentCandidate


async def output(argv, *, timeout=3):
    """Bound output and time, including a child keeping its output pipe open."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=16385,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        async with asyncio.timeout(timeout):
            data = bytearray()
            while chunk := await process.stdout.read(4096):
                data.extend(chunk)
                if len(data) > 16384:
                    raise ValueError("discovery output too large")
            await process.wait()
            if process.returncode:
                raise ValueError("discovery failed")
            return bytes(data)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        # Close owned pipes even if a descendant inherited stdout.
        process._transport.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 1)


def _text(data):
    return data.decode("utf-16" if b"\x00" in data else "utf-8-sig").strip()


PLATFORMS = {"win32": "Windows", "linux": "Linux", "darwin": "macOS"}
NAMES = {"codex": "Codex CLI", "claude_code": "Claude Code"}


def _record(data):
    """The probe prints JSON last; a login shell profile may print text before it."""
    lines = [line for line in _text(data).splitlines() if line.strip()]
    if not lines:
        raise ValueError("empty discovery output")
    return json.loads(lines[-1])


class AgentDiscovery:
    def __init__(self):
        self.candidates = {}
        self.expires = 0

    def resolve(self, identity):
        if time.monotonic() >= self.expires or identity not in self.candidates:
            raise ValueError("unknown or expired local candidate")
        return self.candidates[identity].model_copy(deep=True)

    async def discover(self):
        profiles, hints = {}, []
        # Missing-agent hints are shown only for kinds found nowhere on this PC.
        found_anywhere, missing = set(), []

        async def collect(record, distribution=None, launcher=None):
            wsl = None
            if distribution is not None:
                wsl = WslSettings(
                    distribution=distribution,
                    user=record["user"],
                    python=record["python"],
                    home=record["home"],
                )
            where = (
                f"{distribution} / {record['user']}"
                if wsl
                else PLATFORMS.get(sys.platform, sys.platform)
            )
            found = set()
            for row in record["agents"][:2]:
                kind = row["kind"]
                if kind not in {"codex", "claude_code"}:
                    continue
                try:
                    settings = {"executable": row["executable"]}
                    if wsl:
                        settings["wsl"] = wsl.model_copy(update={"path": row["path"]}).model_dump()
                    profile = AgentProfile.model_validate(
                        {
                            "alias": kind,
                            "kind": kind,
                            "backend": kind,
                            "default_cwd": record["home"],
                            kind: settings,
                        }
                    )
                    argv = [row["executable"], "--version"]
                    if wsl:
                        argv = [
                            launcher,
                            "--distribution",
                            distribution,
                            "--user",
                            wsl.user,
                            "--exec",
                            "/usr/bin/env",
                            "-i",
                            f"HOME={wsl.home}",
                            "PATH=" + ":".join(row["path"]),
                            *argv,
                        ]
                    version = (await output(argv)).decode("utf-8", errors="replace").splitlines()[0]
                    version = "".join(c for c in version if c.isprintable())[:48]
                    identity = secrets.token_hex(16)
                    name = "Codex" if kind == "codex" else "Claude Code"
                    title = f"{name} · {version} · {where}"[:128]
                    profiles[identity] = profile
                    public.append(
                        AgentCandidate(
                            id=identity, kind=kind, title=title, default_cwd=profile.default_cwd
                        )
                    )
                    found.add(kind)
                except (OSError, ValueError, TimeoutError, IndexError):
                    continue
            found_anywhere.update(found)
            shims = record.get("shims", [])
            for kind, name in NAMES.items():
                if kind in found:
                    continue
                if kind == "claude_code" and kind in shims:
                    text = (
                        f"{where}: Claude Code установлен через npm, такой запуск не "
                        "поддерживается. Выполните claude install (нативная сборка) и "
                        "повторите поиск."
                    )
                else:
                    text = (
                        f"{where}: {name} не найден или не запускается. "
                        "Установите CLI локально и повторите поиск."
                    )
                missing.append((kind, text[:256]))

        async def scan_wsl():
            root = os.environ.get("SystemRoot", "")  # noqa: SIM112 -- Windows canonical key
            if not re.fullmatch(r"[A-Za-z]:[\\/][^\x00-\x1f]*", root):
                return
            launcher = str(Path(root) / "System32" / "wsl.exe")
            try:
                listing = _text(await output([launcher, "--list", "--verbose"]))
            except (OSError, ValueError, TimeoutError):
                hints.append("WSL2 недоступен. Установите дистрибутив WSL2 и CLI внутри него.")
                return
            names = re.findall(r"^\s*\*?\s*(.+?)\s{2,}\S+\s+2\s*$", listing, re.MULTILINE)[:8]
            names = [
                name
                for name in names
                if name
                not in {
                    "docker-desktop",
                    "docker-desktop-data",
                    "rancher-desktop",
                    "rancher-desktop-data",
                }
            ]
            source = Path(_discovery_probe.__file__).read_text(encoding="utf-8")

            async def distro(name):
                try:
                    # Validate locally discovered names before passing them to wsl.exe.
                    WslSettings(distribution=name, user="probe")
                    data = await output(
                        [
                            launcher,
                            "--distribution",
                            name,
                            "--exec",
                            "/bin/sh",
                            "-lc",
                            "exec python3 -c " + shlex.quote(source),
                        ],
                        timeout=5,
                    )
                    await collect(_record(data), name, launcher)
                except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                    hints.append(
                        f"{name}: проверьте python3, CLI и пользователя по умолчанию (не root)."[
                            :256
                        ]
                    )

            await asyncio.gather(*(distro(name) for name in names))

        public = []
        try:
            async with asyncio.timeout(16):
                await asyncio.gather(
                    collect(await asyncio.to_thread(_discovery_probe.inventory)),
                    scan_wsl() if sys.platform == "win32" else asyncio.sleep(0),
                )
        except TimeoutError:
            hints.append(
                "Поиск занял слишком долго; показаны доступные результаты. Повторите поиск."
            )
        hints += [text for kind, text in missing if kind not in found_anywhere]
        self.candidates, self.expires = profiles, time.monotonic() + 300
        return public, hints[:32]
