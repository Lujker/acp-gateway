"""Launch the stdlib companion outside the Python environment it will replace."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def start_updater(policy):
    from acp_gateway import remote_update_worker

    policy = policy.expanduser().absolute()
    settings = remote_update_worker.load_policy(policy)
    if sys.platform == "win32":
        from acp_gateway.windows_acl import check_private_paths, ensure_private_directory

        check_private_paths([policy], directories=[policy.parent], purpose="Updater policy")
        ensure_private_directory(Path(settings["spool_dir"]))
    directory = Path(tempfile.mkdtemp(prefix="acpgw-updater-"))
    copy_runtime(directory)
    os.execv(  # noqa: S606 — copied helper and base interpreter
        sys._base_executable, [sys._base_executable, str(directory / "worker.py"), str(policy)]
    )
    return 0


def runtime_sources():
    from acp_gateway import (
        maintenance_worker,
        remote_update_worker,
        update_delivery,
        update_journal,
        updater_windows_acl,
    )

    return (
        (remote_update_worker, "worker.py"),
        (update_journal, "update_journal.py"),
        (maintenance_worker, "update_lock.py"),
        # The copied stdlib worker runs under the independent base interpreter,
        # not the replaceable application's pywin32-enabled environment.
        (updater_windows_acl, "windows_acl.py"),
        (update_delivery, "update_delivery.py"),
    )


def runtime_current(directory):
    return all(
        (directory / name).is_file()
        and not (directory / name).is_symlink()
        and (directory / name).read_bytes() == Path(source.__file__).read_bytes()
        for source, name in runtime_sources()
    )


def copy_runtime(directory):
    for source, name in runtime_sources():
        target = directory / name
        target.write_bytes(Path(source.__file__).read_bytes())
        target.chmod(0o600)


def init_policy(policy, config, env_file, *, role):
    from acp_gateway import paths
    from acp_gateway.config import load_config
    from acp_gateway.connectors.launch import ConnectorLaunch
    from acp_gateway.installation import _uv, installation_info, lease_path

    cfg = load_config(config, env_file)
    info = installation_info()
    if (info["manager"], info["source"]) != ("uv", "registry"):
        raise ValueError("Native updater policy requires a registry uv installation")
    if cfg.config_path is None or cfg.env_file is None:
        raise ValueError("Updater requires persistent config and env files")
    target = ConnectorLaunch.from_saved(cfg).computer_id if role == "connector" else "dispatcher"
    result = subprocess.run(  # noqa: S603 — resolved uv executable, fixed arguments
        [_uv(), "tool", "dir", "--bin"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    entry = Path(result.stdout.strip()) / ("acpgw.exe" if sys.platform == "win32" else "acpgw")
    value = {
        "version": 1,
        "backend": "uv",
        "auto_refresh": True,
        "role": role,
        "target": target,
        "spool_dir": str(
            cfg.settings.updates.spool_dir or cfg.settings.resolved_data_dir() / "update-spool"
        ),
        "entry": str(entry.absolute()),
        "config": str(cfg.config_path.absolute()),
        "env_file": str(cfg.env_file.absolute()),
        "result_dir": str(paths.data_dir() / "updates" / lease_path().stem),
    }
    policy = policy.expanduser().absolute()
    if sys.platform == "win32":
        from acp_gateway.windows_acl import ensure_private_directory

        ensure_private_directory(policy.parent)
    else:
        policy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(policy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        if sys.platform == "win32":
            from acp_gateway.windows_acl import check_private_paths

            check_private_paths([policy], purpose="Updater policy")
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    print(
        f"Updater policy saved: {policy}. Enable updates locally, "
        "then run acpgw updater --policy PATH separately."
    )
    return 0
