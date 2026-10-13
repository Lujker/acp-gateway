"""Host-side Goose bootstrap and local ACP listener lifecycle."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from acp_gateway import paths
from acp_gateway.config import is_loopback_host

INSTALLER = "https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh"
WINDOWS_INSTALLER = "https://raw.githubusercontent.com/aaif-goose/goose/main/download_cli.ps1"


def bin_dir():
    return paths.data_dir() / "tools" / "goose"


def goose_binary():
    name = "goose.exe" if sys.platform == "win32" else "goose"
    candidates = (bin_dir() / name, Path.home() / ".local" / "bin" / name)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
    if found := shutil.which(name):
        return found
    raise ValueError(
        "Goose CLI is missing. Run acpgw agents install goose, then acpgw agents provider"
    )


def _call(command, **kwargs):
    return subprocess.call(command, **kwargs)  # noqa: S603 — validated argv; no shell


def run_bootstrap(args):
    if args.agent_command == "provider":
        return _call([goose_binary(), "configure"])
    if args.version and not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", args.version):
        raise ValueError("Use an exact Goose version such as 1.53.0")
    windows = sys.platform == "win32"
    installer = WINDOWS_INSTALLER if windows else INSTALLER
    print(f"Install Goose {args.version or 'stable'} in {bin_dir()} using {installer}")
    if args.dry_run:
        return 0
    runner = (
        (shutil.which("powershell.exe") or shutil.which("pwsh"))
        if windows
        else shutil.which("bash")
    )
    if runner is None:
        raise ValueError("Goose installer requires PowerShell on Windows or bash on Linux/macOS")
    if not windows:
        missing = [name for name in ("curl", "tar", "bzip2") if shutil.which(name) is None]
        if missing:
            raise ValueError(
                "Install required Goose download/extraction tools with your OS package manager: "
                + ", ".join(missing)
            )
    if not args.yes:
        from acp_gateway.cli.wizard import Prompts

        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("Use --yes to install Goose non-interactively or --dry-run to inspect")
        if not Prompts().yes("Download and run the official Goose installer?", False):
            return 0
    response = httpx.get(installer, follow_redirects=True, timeout=30, trust_env=False)
    response.raise_for_status()
    env = os.environ.copy()
    env.update(CONFIGURE="false", GOOSE_BIN_DIR=str(bin_dir().absolute()), CANARY="false")
    env["GOOSE_WINDOWS_VARIANT"] = "standard"
    env.pop("GOOSE_VERSION", None)
    if args.version:
        env["GOOSE_VERSION"] = args.version
    with tempfile.TemporaryDirectory(prefix="acpgw-goose-install-") as directory:
        script = Path(directory) / ("download_cli.ps1" if windows else "download_cli.sh")
        script.write_bytes(response.content)
        command = [runner, "-NoProfile", "-File", str(script)] if windows else [runner, str(script)]
        result = _call(command, env=env, cwd=directory)
    if result:
        raise ValueError("Goose installer failed; inspect its output and retry")
    print(f"Installed: {goose_binary()}\nNext: acpgw agents provider")
    return 0


def local_goose(cfg, alias):
    try:
        profile = cfg.settings.agent(alias)
    except KeyError:
        raise ValueError("Unknown agent; use acpgw agents list") from None
    parts = urlsplit(profile.url)
    if (
        profile.backend != "remote"
        or profile.kind != "goose"
        or parts.scheme not in {"ws", "http"}
        or not is_loopback_host(parts.hostname or "")
        or parts.path not in {"", "/", "/acp"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Managed Goose needs a direct loopback ws/http profile with /acp path. "
            "Use acpgw agents configure to prepare a local listener"
        )
    secret = cfg.secrets.get(profile.secret_env) if profile.secret_env else None
    if secret is None:
        raise ValueError("Managed Goose requires an agent secret; run acpgw agents configure")
    return profile, parts, secret


def serve(cfg, alias):
    _, parts, secret = local_goose(cfg, alias)
    env = os.environ.copy()
    env["GOOSE_SERVER__SECRET_KEY"] = secret.get_secret_value()
    command = [goose_binary(), "serve", "--host", parts.hostname, "--port", str(parts.port or 80)]
    print(f"Starting authenticated Goose on {parts.hostname}:{parts.port or 80}. Ctrl+C stops it.")
    # Secrets are passed only through the child environment, never through argv or files.
    return _call(command, env=env)


def service(cfg, alias, action, *, config_file=None):
    from acp_gateway.cli.wizard import _atomic
    from acp_gateway.service import GOOSE_MARKER, _quote, _systemctl, unit_path

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", alias):
        raise ValueError("Invalid Goose alias")
    if action == "install":
        profile, _, secret = local_goose(cfg, alias)
    if sys.platform != "linux":
        raise ValueError(
            "Goose service management currently supports Linux/WSL with systemd; "
            "use acpgw agents serve on this platform"
        )
    unit = unit_path().parent / f"acpgw-goose-{alias}.service"
    marker = GOOSE_MARKER
    if unit.is_symlink() or (unit.exists() and not unit.read_text().startswith(marker)):
        raise ValueError("Refusing to modify an unmanaged Goose service")
    config_file = cfg.config_path if cfg is not None else config_file
    if (
        unit.exists()
        and config_file is not None
        and _quote(str(config_file.absolute())) not in unit.read_text()
    ):
        raise ValueError("This Goose service uses another configuration; select its --config first")
    if action == "install":
        goose_binary()
        if cfg.config_path is None or cfg.env_file is None:
            raise ValueError("Persistent config and secrets files are required")
        if (
            dotenv_values(cfg.env_file, interpolate=False).get(profile.secret_env)
            != secret.get_secret_value()
        ):
            raise ValueError("Persist the selected agent key in .env before installing its service")
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command += ["-m", "acp_gateway"]
        command += [
            "--config",
            str(cfg.config_path.absolute()),
            "--env-file",
            str(cfg.env_file.absolute()),
            "agents",
            "serve",
            alias,
        ]
        _systemctl("show-environment")
        search_path = (
            str(Path(goose_binary()).parent) + os.pathsep + os.environ.get("PATH", os.defpath)
        )
        _atomic(
            unit,
            (
                marker + "[Unit]\nDescription=Goose ACP listener\n\n[Service]\n"
                "ExecStart="
                + " ".join(_quote(v) for v in command)
                + "\nEnvironment="
                + _quote("PATH=" + search_path)
                + "\nRestart=on-failure\nRestartSec=5\nUMask=0077\n"
                "\n[Install]\nWantedBy=default.target\n"
            ).encode(),
        )
        _systemctl("daemon-reload")
        print(f"Installed {unit}. Start with acpgw agents service {alias} enable")
    elif action == "status":
        print(
            _systemctl(
                "show",
                unit.name,
                "--property=LoadState,ActiveState,UnitFileState",
                allow_not_found=True,
            ).strip()
        )
    elif action == "logs":
        journal = shutil.which("journalctl")
        if journal is None:
            raise ValueError("journalctl is unavailable")
        return _call([journal, "--user", "--unit", unit.name, "--no-pager", "--lines=50"])
    elif action == "uninstall":
        if unit.exists():
            _systemctl("disable", "--now", unit.name)
            unit.unlink()
            _systemctl("daemon-reload")
    else:
        if not unit.is_file():
            raise ValueError(f"Install first: acpgw agents service {alias} install")
        args = (
            (action, "--now", unit.name) if action in {"enable", "disable"} else (action, unit.name)
        )
        _systemctl(*args)
    return 0


def run(cfg, args):
    if args.agent_command == "list":
        for profile in cfg.settings.agents:
            print(f"{profile.address}: {profile.kind}/{profile.backend} cwd={profile.default_cwd}")
        return 0
    if args.agent_command == "serve":
        return serve(cfg, args.alias)
    return service(cfg, args.alias, args.action)
