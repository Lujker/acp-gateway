"""Installation diagnostics and explicit uv-tool maintenance on Linux/WSL.

Package managers own the environment. Configuration and databases are never
removed here. Runtime leases prevent replacing an environment used by this
version's foreground processes, independently of their selected configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from importlib.metadata import distribution
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version

from acp_gateway import __version__, paths
from acp_gateway.maintenance_worker import lease

PACKAGE = "acp-gateway"
REPOSITORY = "https://github.com/Lujker/acp-gateway"


def _uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise ValueError("uv is unavailable; install uv or use your original package manager")
    return executable


def _tool_directory(executable: str) -> Path:
    try:
        result = subprocess.run(  # noqa: S603 — explicit executable/argv, no shell
            [executable, "tool", "dir"], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        raise ValueError("uv tool dir timed out") from None
    if result.returncode or not result.stdout.strip():
        raise ValueError("cannot locate the uv tool directory")
    return Path(result.stdout.strip()).resolve()


def _source() -> str:
    direct = distribution(PACKAGE).read_text("direct_url.json")
    if not direct:
        return "registry"
    data = json.loads(direct)
    if data.get("dir_info", {}).get("editable"):
        return "editable"
    if "vcs_info" in data:
        return "git"
    return "file"  # Do not expose URLs: private registry/Git credentials may be embedded.


def installation_info() -> dict:
    manager = "unmanaged"
    if getattr(sys, "frozen", False):
        manager = "binary"
    else:
        executable = shutil.which("uv")
        if executable:
            try:
                if Path(sys.prefix).resolve() == _tool_directory(executable) / PACKAGE:
                    manager = "uv"
            except ValueError:
                pass
    return {"version": __version__, "manager": manager, "source": _source()}


@contextmanager
def runtime_lease(*, exclusive=False):
    """One shared lease per process; maintenance needs the exclusive lease."""
    with lease(lease_path(), exclusive=exclusive) as descriptor:
        yield descriptor


def lease_path() -> Path:
    identity = str(Path(sys.prefix).resolve())
    if getattr(sys, "frozen", False):
        identity = str(Path(sys.executable).resolve())
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return paths.data_dir() / "installation-locks" / f"{key}.lock"


def _service_preflight(*, uninstall: bool) -> list[str]:
    if sys.platform != "linux":
        return []  # Native service adapters remain separate P3.2/P4.6 milestones.
    from acp_gateway import service

    installed = []
    for role in ("gateway", "connector"):
        path = service.unit_path(role)
        if not path.exists() and not path.is_symlink():
            continue
        service._owned(path, role)
        expected = "ExecStart=" + service._quote(sys.executable) + " "
        if not any(line.startswith(expected) for line in path.read_text().splitlines()):
            raise ValueError(
                "managed unit belongs to another installation; manage its service explicitly"
            )
        state = service._systemctl(
            "show", service._unit(role), "--property=ActiveState", "--value"
        ).strip()
        if state not in {"inactive", "failed"}:
            selector = " --role connector" if role == "connector" else ""
            raise ValueError(f"stop the service first: acpgw service{selector} stop")
        installed.append(role)
    if uninstall:
        for role in installed:
            service.control("uninstall", role=role)
    return installed


def _handoff(args, command: list[str], descriptor: int) -> None:
    from acp_gateway import maintenance_worker
    from acp_gateway.config import load_config

    prefix = Path(sys.prefix).resolve()
    bin_result = subprocess.run(  # noqa: S603 — resolved uv executable
        [_uv(), "tool", "dir", "--bin"], capture_output=True, text=True, timeout=10, check=True
    )
    entry = Path(bin_result.stdout.strip()) / ("acpgw.exe" if os.name == "nt" else "acpgw")
    if not entry.is_file():
        raise ValueError("uv entry point is missing")
    cfg = load_config(args.config, args.env_file)
    database = cfg.settings.resolved_data_dir().resolve() / "gateway.db"
    backup = paths.data_dir() / "updates" / lease_path().stem / uuid.uuid4().hex
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = {
        "operation": "rollback" if args.command == "update" and args.rollback else args.command,
        "command": command,
        "prefix": str(prefix),
        "entry": str(entry.absolute()),
        "database": str(database),
        "config": str(cfg.config_path.resolve()) if cfg.config_path else None,
        "env_file": str(cfg.env_file.resolve()) if cfg.env_file else None,
        "expected_version": str(Version(args.version))
        if args.command == "update" and args.version
        else None,
        "backup": str(backup),
        "current": str(backup.parent / "current.json"),
        "lease": str(lease_path()),
        "pid": os.getpid(),
    }
    temporary = Path(tempfile.mkdtemp(prefix="acpgw-maintenance-"))
    try:
        helper = temporary / "worker.py"
        helper.write_text(
            Path(maintenance_worker.__file__).read_text(encoding="utf-8"), encoding="utf-8"
        )
        manifest = temporary / "plan.json"
        manifest.write_text(json.dumps(plan), encoding="utf-8")
        manifest.chmod(0o600)
        base_python = str(Path(sys._base_executable).resolve())
        if sys.platform != "win32":
            os.set_inheritable(descriptor, True)
        print("Private recovery snapshot and post-install health checks are enabled.", flush=True)
        os.execv(base_python, [base_python, str(helper), str(manifest)])  # noqa: S606 — base interpreter
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def check_update() -> int:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(f"https://pypi.org/pypi/{PACKAGE}/json")
    if response.status_code == 404:
        print("No PyPI release is published yet. Install a reviewed release wheel or Git ref.")
        return 0
    response.raise_for_status()
    data = response.json()
    urls = data.get("info", {}).get("project_urls") or {}
    if not any(str(value).rstrip("/") == REPOSITORY for value in urls.values()):
        raise ValueError("PyPI package does not identify this repository; refusing this channel")
    candidates = []
    for text, files in data.get("releases", {}).items():
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        if (
            not version.is_prerelease
            and not version.is_devrelease
            and any(not file.get("yanked", False) for file in files)
        ):
            candidates.append(version)
    if not candidates:
        print("No stable PyPI release is available.")
        return 0
    latest = max(candidates)
    print(f"Installed: {__version__}; latest stable PyPI release: {latest}")
    print("Update available." if latest > Version(__version__) else "No newer stable release.")
    print("This checks PyPI; file/Git installations require an explicit update --from source.")
    return 0


def maintenance(args) -> int:
    info = installation_info()
    if args.command == "version":
        print(
            json.dumps(info)
            if args.json
            else f"acpgw {info['version']} (manager: {info['manager']}, source: {info['source']})"
        )
        return 0
    if args.command == "update" and args.check:
        if args.constraints:
            raise ValueError("--constraints requires --from")
        return check_update()
    if info["manager"] != "uv":
        raise ValueError(
            f"installation manager is {info['manager']}; internal maintenance requires uv tool. "
            "For Docker use compose pull/up; for Git checkout use scripts/install.sh; "
            "for other installations use the original package manager"
        )
    executable = _uv()
    if args.command == "uninstall":
        command = [executable, "tool", "uninstall", PACKAGE]
    elif args.rollback:
        command = []
    elif args.source or args.version:
        target = args.source
        if args.version:
            target = f"{PACKAGE}=={Version(args.version)}"
        else:
            if target.startswith("-") or any(ord(char) < 32 for char in target):
                raise ValueError("invalid installation source")
            if Path(target).exists():
                target = Path(target).resolve().as_uri()
            if target != PACKAGE and not target.startswith(
                ("https://", "file://", "git+https://", "git+ssh://", "git+file://")
            ):
                raise ValueError("source must be a wheel path/HTTPS URL or an explicit Git URL")
            if target != PACKAGE:
                target = f"{PACKAGE} @ {target}"
        command = [executable, "tool", "install", "--force", "--python", "3.12", target]
    else:
        if info["source"] != "registry":
            raise ValueError("file/Git/editable installations require update --from SOURCE")
        command = [executable, "tool", "upgrade", PACKAGE]
    if args.command == "update" and args.constraints:
        if not args.source or not args.constraints.is_file():
            raise ValueError("--constraints requires --from and an existing requirements file")
        command += ["--constraints", str(args.constraints.resolve())]
    if args.dry_run:
        # Do not print a source URL, which could contain credentials.
        print(f"Would {args.command} the uv tool; configuration, secrets and data are kept.")
        return 0
    with runtime_lease(exclusive=True) as descriptor:
        _service_preflight(uninstall=args.command == "uninstall")
        print("Configuration, secrets and data are kept. Handing maintenance to uv.", flush=True)
        _handoff(args, command, descriptor)
    return 0
