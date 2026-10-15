"""Installation diagnostics and explicit uv-tool maintenance.

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
    if sys.platform == "win32":
        from acp_gateway.windows_service import _request

        task = _request("status")
        if task["installed"]:
            if task["enabled"] or task["state"] in {2, 4}:
                raise ValueError(
                    "disable the connector task before maintenance: "
                    "acpgw service --role connector disable"
                )
            if uninstall:
                _request("uninstall")
        return []
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
    goose_units = []
    for path in sorted(service.unit_path().parent.glob("acpgw-goose-*.service")):
        if path.is_symlink() or not path.read_text().startswith(service.GOOSE_MARKER):
            raise ValueError("unmanaged Goose unit; manage its service explicitly")
        expected = "ExecStart=" + service._quote(sys.executable) + " "
        if not any(line.startswith(expected) for line in path.read_text().splitlines()):
            raise ValueError(
                "Goose unit belongs to another installation; manage its service explicitly"
            )
        state = service._systemctl("show", path.name, "--property=ActiveState", "--value").strip()
        if state not in {"inactive", "failed"}:
            alias = path.name.removeprefix("acpgw-goose-").removesuffix(".service")
            raise ValueError(f"stop the service first: acpgw agents service {alias} stop")
        goose_units.append(path)
    if uninstall:
        for role in installed:
            service.control("uninstall", role=role)
        for path in goose_units:
            service._systemctl("disable", "--now", path.name)
            path.unlink()
        if goose_units:
            service._systemctl("daemon-reload")
    return installed


def _handoff(args, command: list[str], descriptor: int) -> None:
    from acp_gateway import maintenance_worker

    prefix = Path(sys.prefix).resolve()
    bin_result = subprocess.run(  # noqa: S603 — resolved uv executable
        [_uv(), "tool", "dir", "--bin"], capture_output=True, text=True, timeout=10, check=True
    )
    entry = Path(bin_result.stdout.strip()) / ("acpgw.exe" if os.name == "nt" else "acpgw")
    if not entry.is_file():
        raise ValueError("uv entry point is missing")
    # Resolve configuration in a short-lived process: importing pydantic here
    # would map its extension DLL in the waiting Windows caller's environment.
    probe = (
        "import json,sys; from pathlib import Path; "
        "from acp_gateway.config import load_config; "
        "cfg=load_config(*(Path(p) if p else None for p in json.loads(sys.argv[1]))); "
        "print(json.dumps({'database':str(cfg.settings.resolved_data_dir().resolve()/'gateway.db'),"
        "'config':str(cfg.config_path.resolve()) if cfg.config_path else None,"
        "'env_file':str(cfg.env_file.resolve()) if cfg.env_file else None}))"
    )
    selected = json.dumps(
        [str(args.config) if args.config else None, str(args.env_file) if args.env_file else None]
    )
    result = subprocess.run(  # noqa: S603 — installed interpreter and fixed probe
        [sys.executable, "-c", probe, selected], capture_output=True, text=True, timeout=30
    )
    if result.returncode:
        raise ValueError("cannot resolve configuration for a database recovery snapshot")
    configuration = json.loads(result.stdout)
    backup = paths.data_dir() / "updates" / lease_path().stem / uuid.uuid4().hex
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = {
        "operation": "rollback" if args.command == "update" and args.rollback else args.command,
        "command": command,
        "prefix": str(prefix),
        "entry": str(entry.absolute()),
        **configuration,
        "expected_version": str(Version(args.version))
        if args.command == "update" and args.version
        else None,
        "backup": str(backup),
        "current": str(backup.parent / "current.json"),
        "lease": str(lease_path()),
        "pid": os.getpid(),
        "result": str(backup.parent / (backup.name + ".result.json")),
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
        print("Private recovery snapshot and post-install health checks are enabled.", flush=True)
        maintenance_worker.record_result(plan, "queued")
        latest = backup.parent / "last-operation.json"
        pointer = latest.with_suffix(".tmp")
        pointer.write_text(json.dumps({"result": plan["result"]}), encoding="utf-8")
        pointer.chmod(0o600)
        pointer.replace(latest)
        if sys.platform == "win32":
            if args.handoff_file:
                with args.handoff_file.open("x", encoding="utf-8") as stream:
                    args.handoff_file.chmod(0o600)
                    json.dump(
                        {"python": base_python, "worker": str(helper), "manifest": str(manifest)},
                        stream,
                    )
                return
            subprocess.Popen(  # noqa: S603 — base interpreter and private helper
                [base_python, str(helper), str(manifest)],
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            print(
                "Maintenance queued on Windows; use update --status for the verified result.",
                flush=True,
            )
            print("ACPGW_MAINTENANCE_RESULT=" + plan["result"], flush=True)
            return
        os.set_inheritable(descriptor, True)
        os.execv(base_python, [base_python, str(helper), str(manifest)])  # noqa: S606 — base interpreter
    except BaseException:
        maintenance_worker.record_result(plan, "failed", 1)
        shutil.rmtree(temporary, ignore_errors=True)
        raise


@contextmanager
def _service_update_guard(args):
    if sys.platform == "win32":
        directory = paths.data_dir() / "updates" / lease_path().stem
        pending = directory / "service-operation.json"
        proof = getattr(args, "service_coordinator_file", None)
        if proof is not None:
            from acp_gateway.windows_acl import check_private_paths

            marker = json.loads(pending.read_text(encoding="utf-8"))
            if proof.resolve() != Path(marker["coordinator_file"]).resolve():
                raise ValueError("Invalid service coordinator proof")
            check_private_paths([proof], directories=[proof.parent], purpose="Service coordinator")
            if json.loads(proof.read_text(encoding="utf-8")) != {"token": marker["token"]}:
                raise ValueError("Invalid service coordinator proof")
            try:
                with lease(lease_path().with_suffix(".service-update.lock"), exclusive=True):
                    owned = False
            except ValueError:
                owned = True
            if not owned:
                raise ValueError("Service coordinator no longer owns its lock")
            yield None
            return
        with lease(lease_path().with_suffix(".service-update.lock"), exclusive=True) as descriptor:
            if pending.exists():
                raise ValueError("A service update is already pending; inspect update --status")
            pointer = directory / "last-operation.json"
            if pointer.exists():
                result = Path(json.loads(pointer.read_text(encoding="utf-8"))["result"])
                if result.parent != directory:
                    raise ValueError("Invalid maintenance result pointer")
                if json.loads(result.read_text(encoding="utf-8"))["status"] in {
                    "queued",
                    "running",
                }:
                    raise ValueError("Package maintenance is already pending")
            yield descriptor
        return
    if sys.platform != "linux":
        yield None
        return
    path = lease_path().with_suffix(".service-update.lock")
    inherited = getattr(args, "service_coordinator_fd", None)
    if inherited is not None:
        import fcntl

        expected, opened = path.stat(), os.fstat(inherited)
        if (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("invalid service coordinator descriptor")
        fcntl.flock(inherited, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield inherited
    else:
        with lease(path, exclusive=True) as descriptor:
            yield descriptor


def _handoff_service_update(args, descriptor):
    from acp_gateway import service_update, service_update_worker

    try:
        args.config, args.env_file, args.service_role = service_update.resolve_selection(
            args.config, args.env_file, args.service_role
        )
        inspected = service_update.inspect_service(args.config, args.env_file, args.service_role)
        service_update.preflight_activity(
            inspected, restart_uncertain=getattr(args, "restart_uncertain", False)
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        directory = paths.data_dir() / "updates" / lease_path().stem
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = directory / (uuid.uuid4().hex + ".service-result.json")
        # Exception text can contain config values. Persist only controlled guidance.
        service_update_worker.record(
            {"result": str(result)},
            "failed",
            "preflight",
            1,
            failed_phase="preflight",
            package_status="not_started",
            error_type=type(exc).__name__,
            reason="Preflight refused. Check selected config/env and connector activity; "
            "the immediate CLI error gives details. No service was stopped.",
        )
        pointer = directory / "last-operation.json"
        temporary_pointer = pointer.with_suffix(".tmp")
        temporary_pointer.write_text(json.dumps({"result": str(result)}), encoding="utf-8")
        temporary_pointer.chmod(0o600)
        temporary_pointer.replace(pointer)
        raise
    result = subprocess.run(  # noqa: S603 — resolved uv executable and fixed arguments
        [_uv(), "tool", "dir", "--bin"], capture_output=True, text=True, timeout=10, check=True
    )
    entry = Path(result.stdout.strip()) / ("acpgw.exe" if sys.platform == "win32" else "acpgw")
    if not entry.is_file():
        raise ValueError("uv entry point is missing")
    directory = paths.data_dir() / "updates" / lease_path().stem
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix="acpgw-service-update-"))
    windows = sys.platform == "win32"
    proof = temporary / "coordinator.json"
    token = uuid.uuid4().hex
    arguments = (
        ["update", "--service-coordinator-file", str(proof)]
        if windows
        else ["update", "--service-coordinator-fd", str(descriptor)]
    )
    if args.rollback:
        arguments += ["--rollback"]
    if args.source:
        arguments += ["--from", args.source]
    if args.version:
        arguments += ["--version", args.version]
    if args.constraints:
        arguments += ["--constraints", str(args.constraints.absolute())]
    plan = {
        "entry": str(entry.absolute()),
        "python": sys.executable,
        "arguments": arguments,
        "selection": [
            str(args.config.absolute()) if args.config else None,
            str(args.env_file.absolute()) if args.env_file else None,
            args.service_role,
        ],
        "coordinator_fd": descriptor,
        "restart_uncertain": getattr(args, "restart_uncertain", False),
        "coordinator_lock": str(lease_path().with_suffix(".service-update.lock")),
        "pid": os.getpid(),
        "reservation": str(temporary / "reservation.json"),
        "result": str(directory / (uuid.uuid4().hex + ".service-result.json")),
        "pointer": str(directory / "last-operation.json"),
        "pending_pointer": str(directory / "service-operation.json"),
    }
    try:
        helper, manifest = temporary / "worker.py", temporary / "plan.json"
        helper.write_text(
            Path(service_update_worker.__file__).read_text(encoding="utf-8"), encoding="utf-8"
        )
        if windows:
            from acp_gateway import maintenance_worker, powershell, windows_service

            plan["windows_script"] = windows_service.SCRIPT
            (temporary / "support_worker.py").write_text(
                Path(maintenance_worker.__file__).read_text(encoding="utf-8"), encoding="utf-8"
            )
            (temporary / "ps_support.py").write_text(
                Path(powershell.__file__).read_text(encoding="utf-8"), encoding="utf-8"
            )
            proof.write_text(json.dumps({"token": token}), encoding="utf-8")
        manifest.write_text(json.dumps(plan), encoding="utf-8")
        manifest.chmod(0o600)
        service_update_worker.record(plan, "queued", "inspect")
        pointer = Path(plan["pending_pointer"])
        pointer.write_text(
            json.dumps(
                {
                    "result": plan["result"],
                    **({"coordinator_file": str(proof), "token": token} if windows else {}),
                }
            ),
            encoding="utf-8",
        )
        pointer.chmod(0o600)
        if windows:
            handoff = {
                "python": sys._base_executable,
                "worker": str(helper),
                "manifest": str(manifest),
            }
            if args.handoff_file:
                with args.handoff_file.open("x", encoding="utf-8") as stream:
                    json.dump(handoff, stream)
            else:
                subprocess.Popen(  # noqa: S603 — copied helper and base interpreter
                    [handoff["python"], handoff["worker"], handoff["manifest"]],
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                )
                print("Service update queued; use update --status for the complete result.")
                print("ACPGW_MAINTENANCE_RESULT=" + plan["result"])
            return
        os.set_inheritable(descriptor, True)
        os.execv(sys._base_executable, [sys._base_executable, str(helper), str(manifest)])  # noqa: S606
    except BaseException:
        service_update_worker.record(plan, "failed", "handoff", 1)
        Path(plan["pending_pointer"]).unlink(missing_ok=True)
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def latest_stable_release() -> Version | None:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(f"https://pypi.org/pypi/{PACKAGE}/json")
    if response.status_code == 404:
        print("No PyPI release is published yet. Install a reviewed release wheel or Git ref.")
        return None
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
    return max(candidates, default=None)


def check_update() -> int:
    latest = latest_stable_release()
    if latest is None:
        print("No stable PyPI release is available.")
        return 0
    print(f"Installed: {__version__}; latest stable PyPI release: {latest}")
    print("Update available." if latest > Version(__version__) else "No newer stable release.")
    print("This checks PyPI; file/Git installations require an explicit update --from source.")
    return 0


def maintenance(args) -> int:
    with_services = getattr(args, "with_services", False)
    if with_services and (
        sys.platform not in {"linux", "win32"}
        or args.check
        or args.status
        or (args.handoff_file and sys.platform != "win32")
    ):
        raise ValueError("--with-services requires a Linux or Windows update/rollback operation")
    if getattr(args, "restart_uncertain", False) and (
        not with_services or args.service_role == "gateway" or args.check or args.status
    ):
        raise ValueError("--restart-uncertain requires a connector update --with-services")
    if getattr(args, "service_role", None) and not with_services:
        raise ValueError("--service-role requires --with-services")
    if with_services and sys.platform == "win32" and args.service_role == "gateway":
        raise ValueError("Windows managed update supports the connector task only")
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
    if args.command == "update" and args.status:
        directory = paths.data_dir() / "updates" / lease_path().stem
        pointer = directory / "service-operation.json"
        if not pointer.is_file():
            pointer = directory / "last-operation.json"
        if not pointer.is_file():
            print("No recorded maintenance operation.")
            return 0
        result = Path(json.loads(pointer.read_text(encoding="utf-8"))["result"])
        if result.parent != directory:
            raise ValueError("invalid maintenance result pointer")
        info = json.loads(result.read_text(encoding="utf-8"))
        print(json.dumps(info))
        return 0 if info["status"] == "succeeded" else 1 if info["status"] == "failed" else 2
    if args.handoff_file and sys.platform != "win32":
        raise ValueError("external handoff is used only by the Windows installer")
    if info["manager"] != "uv":
        raise ValueError(
            f"installation manager is {info['manager']}; internal maintenance requires uv tool. "
            "For Docker use compose pull/up; for Git checkout use scripts/install.sh; "
            "for other installations use the original package manager"
        )
    if getattr(args, "stable", False):
        if info["source"] != "registry":
            raise ValueError("--stable requires a registry installation; use update --from SOURCE")
        if args.constraints:
            raise ValueError("--constraints requires --from")
        latest = latest_stable_release()
        if latest is None or latest <= Version(info["version"]):
            print("No newer stable release. Package and services are unchanged.")
            return 0
        # Resolve once before any service stop. The coordinator receives the exact
        # version, avoiding a second lookup or uv's saved ==version constraint.
        args.version = str(latest)
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
        command = [
            executable,
            "tool",
            "install",
            "--force",
            "--python",
            "3.12",
            target,
            "--refresh-package",
            PACKAGE,
        ]
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
        print(
            "Would reserve, stop, update and restore the selected managed service."
            if with_services
            else f"Would {args.command} the uv tool; configuration, secrets and data are kept."
        )
        return 0
    if with_services:
        with _service_update_guard(args) as descriptor:
            _handoff_service_update(args, descriptor)
        return 0
    with _service_update_guard(args) as coordinator, runtime_lease(exclusive=True) as descriptor:
        _service_preflight(uninstall=args.command == "uninstall")
        if coordinator is not None:
            os.set_inheritable(coordinator, True)
        print("Configuration, secrets and data are kept. Handing maintenance to uv.", flush=True)
        _handoff(args, command, descriptor)
    return 0
