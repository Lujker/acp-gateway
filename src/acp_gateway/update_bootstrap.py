"""Automatic independent updater preparation; call only on setup or actual start.

Read-only status/version/check commands must never call ensure_updates.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from acp_gateway.config import load_config
from acp_gateway.installation import _uv, installation_info
from acp_gateway.remote_update_worker import load_policy
from acp_gateway.service_update import inspect_service
from acp_gateway.update_runner import copy_runtime, init_policy, runtime_current
from acp_gateway.updater_service import _private_directory, manage, runtime_path


@dataclass(frozen=True)
class BootstrapResult:
    ready: bool
    reason: str
    policy: Path | None = None


def validate_policy(path, config, *, role, target):
    value = load_policy(path)
    expected = {
        "role": role,
        "target": target,
        "config": str(config.config_path.absolute()),
        "env_file": str(config.env_file.absolute()),
        "spool_dir": str(
            config.settings.updates.spool_dir
            or config.settings.resolved_data_dir() / "update-spool"
        ),
    }
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("Existing updater policy belongs to a different configuration or target")


def worker_ready(config, *, role, target):
    from acp_gateway.update_service import LocalUpdates

    return LocalUpdates(config, role=role, target=target).available("uv")


def ensure_companion(path):
    if not runtime_current(runtime_path(path)):
        if runtime_path(path).exists():
            manage("stop", path)
        manage("install", path)
    try:
        manage("enable", path)
    except ValueError:
        # A missing autostart definition can be repaired without changing policy.
        manage("install", path)
        manage("enable", path)


def ensure_updates(config, *, role, target=None, timeout=5):
    """Prepare exactly the selected role. Return a truthful, non-fatal startup result."""
    if not config.settings.updates.enabled:
        return BootstrapResult(False, "disabled")
    if role not in {"dispatcher", "connector"}:
        raise ValueError("Updater role must be dispatcher or connector")
    info = installation_info()
    if sys.platform not in {"linux", "win32"} or (info["manager"], info["source"]) != (
        "uv",
        "registry",
    ):
        return BootstrapResult(
            False,
            "Unsupported installation: requires registry uv on Linux/Windows "
            "or a Docker host companion",
        )
    if sys.platform == "win32" and role == "dispatcher":
        return BootstrapResult(False, "Windows managed dispatcher service is unsupported")
    if config.config_path is None or config.env_file is None:
        return BootstrapResult(False, "Updater requires persistent config and env files")
    try:
        inspect_service(
            str(config.config_path),
            str(config.env_file),
            "gateway" if role == "dispatcher" else "connector",
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return BootstrapResult(False, str(exc))
    if role == "dispatcher":
        target = "dispatcher"
    elif target is None:
        from acp_gateway.connectors.launch import ConnectorLaunch

        target = ConnectorLaunch.from_saved(config).computer_id
    if not isinstance(target, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", target):
        raise ValueError("Invalid updater target")
    path = (
        config.settings.resolved_data_dir()
        / "update-companions"
        / f"{role}-{target}"
        / "updater.json"
    )
    try:
        if not path.exists():
            init_policy(path, config.config_path, config.env_file, role=role)
        validate_policy(path, config, role=role, target=target)
        ensure_companion(path)
        deadline = time.monotonic() + timeout
        while not worker_ready(config, role=role, target=target):
            if time.monotonic() >= deadline:
                return BootstrapResult(
                    False, "Updater autostart prepared but worker readiness was not observed", path
                )
            time.sleep(0.1)
        return BootstrapResult(True, "ready", path)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return BootstrapResult(False, str(exc), path)


def docker_executable():
    executable = shutil.which("docker")
    if executable is None:
        raise ValueError("Docker host CLI is unavailable")
    return str(Path(executable).absolute())


def policy_worker_ready(value):
    from acp_gateway.update_journal import Journal

    path = Path(value["spool_dir"]) / "operations.db"
    if not path.is_file():
        return False
    journal = Journal(path)
    try:
        worker = journal.worker()
        return bool(
            isinstance(worker, dict)
            and all(worker.get(key) == value[key] for key in ("role", "target", "backend"))
            and type(worker.get("published")) in {int, float}
            and 0 <= time.time() - worker["published"] <= 10
        )
    finally:
        journal.close()


def ensure_docker_host(project_dir, *, compose_files=None, timeout=0):
    """Standard /config + /data deployment only. Run on the Linux Docker host."""
    if sys.platform != "linux":
        return BootstrapResult(False, "Docker host updater supports Linux only")
    project = Path(project_dir).expanduser().resolve()
    cfg = load_config(project / "runtime/gateway.yaml", project / "runtime/gateway.env")
    if not cfg.settings.updates.enabled:
        return BootstrapResult(False, "disabled")
    if cfg.settings.resolved_data_dir() != Path("/data") or cfg.settings.updates.spool_dir not in {
        None,
        Path("/data/update-spool"),
    }:
        return BootstrapResult(
            False, "Automatic Docker bootstrap requires the standard /data mailbox mount"
        )
    files = [Path(item).resolve() for item in (compose_files or [project / "compose.yaml"])]
    if not 1 <= len(files) <= 8 or any(not item.is_file() for item in files):
        return BootstrapResult(False, "Docker bootstrap requires existing selected Compose files")
    private = project / "private-updater"
    path = private / "updater.json"
    try:
        value = {
            "version": 1,
            "backend": "docker",
            "role": "dispatcher",
            "target": "dispatcher",
            "delivery": "official",
            "images": {},
            "docker": docker_executable(),
            "auto_refresh": True,
            "refresh_tool": _uv(),
            "project_dir": str(project),
            "compose_files": [str(item) for item in files],
            "service": "gateway",
            "config": "/config/gateway.yaml",
            "env_file": "/config/gateway.env",
            "container_spool": "/data/update-spool",
            "spool_dir": str(project / "state/update-spool"),
            "database": str(project / "state/gateway.db"),
            "work_dir": str(private / "backups"),
            "override_file": str(private / "image.json"),
            "image_env": str(project / ".env"),
        }
        _private_directory(private)
        if path.exists():
            existing = load_policy(path)
            if any(existing.get(key) != item for key, item in value.items() if key != "images"):
                raise ValueError(
                    "Existing Docker policy differs from the deployment; refusing overwrite"
                )
        else:
            with os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8"
            ) as stream:
                json.dump(value, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        ensure_companion(path)
        deadline = time.monotonic() + timeout
        while not policy_worker_ready(value):
            if time.monotonic() >= deadline:
                return BootstrapResult(
                    False,
                    "Docker host companion prepared; "
                    "start the selected Compose gateway and verify readiness",
                    path,
                )
            time.sleep(0.1)
        return BootstrapResult(True, "ready", path)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return BootstrapResult(False, str(exc), path)


def stage_runtime(policy):
    """Export this installed package's helper code outside the replaced environment.

    Safe while an operation is active: only the worker swaps files, after closing
    its journal/heartbeat and releasing its lease. No service or install action.
    """
    policy = Path(policy).expanduser().absolute()
    _private_directory(policy.parent)
    runtime = runtime_path(policy)
    staged = runtime.with_name(runtime.name + ".next")
    from acp_gateway.maintenance_worker import lease

    with lease(runtime.with_name(runtime.name + ".stage.lock"), exclusive=True):
        if staged.is_symlink():
            raise ValueError("Refusing symlinked staged updater runtime")
        temporary = Path(tempfile.mkdtemp(prefix="updater-stage-", dir=policy.parent))
        try:
            copy_runtime(temporary)
            manifest = {
                "files": {
                    item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                    for item in temporary.iterdir()
                }
            }
            (temporary / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            if staged.exists():
                shutil.rmtree(staged)
            temporary.replace(staged)
            return staged
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Internal independent updater bootstrap")
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage-runtime")
    stage.add_argument("policy", type=Path)
    docker = commands.add_parser("docker-host")
    docker.add_argument("project", type=Path)
    docker.add_argument("--compose-file", action="append", type=Path)
    docker.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args(argv)
    if args.command == "stage-runtime":
        stage_runtime(args.policy)
        return 0
    result = ensure_docker_host(args.project, compose_files=args.compose_file, timeout=args.timeout)
    print(
        json.dumps(
            {
                "ready": result.ready,
                "reason": result.reason,
                "policy": str(result.policy) if result.policy else None,
            }
        )
    )
    return (
        0
        if result.ready or result.reason == "disabled"
        else 2
        if result.policy and result.reason.startswith("Docker host companion prepared;")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
